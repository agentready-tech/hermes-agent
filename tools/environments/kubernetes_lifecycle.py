"""Lifecycle of an enrolled AgentSee sandbox StatefulSet, shared across Hermes processes."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from tools.environments.base import EnvironmentConnectionError
from tools.environments.remote_common import run_capture

_READY_TIMEOUT = 30.0
_RETRY_INTERVAL = 0.25


class SandboxLifecycle:
    def __init__(self, home: Path, executable: str, namespace: str, idle_seconds: int):
        ident = re.fullmatch(r"env_([a-f0-9]{32})", home.name)
        owned_namespace = f"agentsee-a-{ident[1]}" if ident else "agentsee-mike" if home.name == "mike" else None
        if namespace != owned_namespace:
            raise ValueError("On-demand Kubernetes sandbox must match the managed profile namespace")
        if idle_seconds < 0:
            raise ValueError("Kubernetes sandbox idle timeout must be non-negative")
        self.home = home
        self.executable = executable
        self.namespace = namespace
        self.idle_seconds = idle_seconds
        self.root = home / "kubernetes-sandbox"
        self.activity = self.root / "activity"
        self.scale_path = f"/apis/apps/v1/namespaces/{namespace}/statefulsets/sandbox/scale"

    @contextmanager
    def _lock(self, name: str, flags: int):
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            raise EnvironmentConnectionError("Kubernetes sandbox lifecycle requires POSIX file locks")
        import fcntl

        uid = geteuid()
        self.root.mkdir(mode=0o700, exist_ok=True)
        directory = self.root.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077 or directory.st_uid != uid:
            raise EnvironmentConnectionError("Unsafe Kubernetes sandbox lifecycle directory")
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o077:
                raise EnvironmentConnectionError("Unsafe Kubernetes sandbox lifecycle lock")
            fcntl.flock(fd, flags)
            yield fd
        finally:
            os.close(fd)

    def _scale(self) -> tuple[int, str]:
        try:
            response = run_capture([self.executable, "get", "--raw", self.scale_path], timeout=8, check=True)
            data = json.loads(response.stdout)
            spec = data["spec"]
            if not isinstance(spec, dict):
                raise ValueError("Invalid sandbox scale")
            # Kubernetes omits the zero-valued ScaleSpec.replicas field.
            replicas, version = spec.get("replicas", 0), data["metadata"]["resourceVersion"]
            if type(replicas) is not int or replicas not in (0, 1) or not isinstance(version, str) or not version:
                raise ValueError("Invalid sandbox scale")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            raise EnvironmentConnectionError("Cannot read the managed Kubernetes sandbox scale") from None
        return replicas, version

    def _set_scale(self, replicas: int, version: str) -> None:
        spec = {"apiVersion": "autoscaling/v1", "kind": "Scale",
                "metadata": {"name": "sandbox", "namespace": self.namespace, "resourceVersion": version},
                "spec": {"replicas": replicas}}
        try:
            subprocess.run([self.executable, "replace", "--raw", self.scale_path, "-f", "-"],
                           input=json.dumps(spec), text=True, capture_output=True, check=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            raise EnvironmentConnectionError("Cannot change the managed Kubernetes sandbox scale") from None

    def _pod(self) -> dict:
        from tools.environments.kubernetes import read_pod

        return read_pod(self.executable, self.namespace, "sandbox-0", missing_ok=True)

    def identity(self) -> dict:
        """Verify that the current Pod can execute requests, without changing it."""
        from tools.environments.kubernetes import pod_identity

        pod = self._pod()
        if not any(condition.get("type") == "Ready" and condition.get("status") == "True"
                   for condition in pod.get("status", {}).get("conditions", ())):
            raise EnvironmentConnectionError("The Kubernetes sandbox is not ready")
        return pod_identity(pod, self.namespace, "sandbox-0", "sandbox")

    def _wait_absent(self) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT
        while self._pod():
            if time.monotonic() >= deadline:
                raise EnvironmentConnectionError("The previous Kubernetes sandbox is still stopping")
            time.sleep(_RETRY_INTERVAL)

    def _start_locked(self) -> dict:
        replicas, _ = self._scale()
        if replicas == 0:
            # A new request during downscale waits for the old Pod to disappear. Before then
            # a stale Ready response or the stable sandbox-0 name cannot identify its replacement.
            self._wait_absent()
            replicas, version = self._scale()
            if replicas == 0:
                self._set_scale(1, version)
        deadline = time.monotonic() + _READY_TIMEOUT
        while True:
            try:
                return self.identity()
            except EnvironmentConnectionError:
                if time.monotonic() >= deadline:
                    raise EnvironmentConnectionError("The Kubernetes sandbox did not become ready") from None
            time.sleep(_RETRY_INTERVAL)

    @contextmanager
    def active(self, *, wake: bool = True, expected_uid: str | None = None):
        """Pin the sandbox while a real command is running, including in separate MCP processes."""
        import fcntl

        # The active flock is acquired under the transition flock. The reaper uses the
        # same ordering and cannot reserve a stopped Pod between admission and execution.
        with ExitStack() as stack:
            with self._lock("transition.lock", fcntl.LOCK_EX):
                if not wake and self._scale()[0] == 0:
                    raise EnvironmentConnectionError("The Kubernetes sandbox is stopping")
                identity = self._start_locked() if wake else self.identity()
                if expected_uid and identity["uid"] != expected_uid:
                    raise EnvironmentConnectionError("The Kubernetes sandbox changed before the command")
                fd = stack.enter_context(self._lock("active.lock", fcntl.LOCK_SH))
                self.activity.touch(mode=0o600)
            try:
                yield identity, fd
            finally:
                self.activity.touch(mode=0o600)

    def stop_if_idle(self) -> bool:
        import fcntl
        from tools.bot_desktop import lease

        try:
            if time.time() - self.activity.stat().st_mtime < self.idle_seconds:
                return False
            with self._lock("transition.lock", fcntl.LOCK_EX | fcntl.LOCK_NB):
                with self._lock("active.lock", fcntl.LOCK_EX | fcntl.LOCK_NB):
                    path = lease._path(str(self.home))
                    with lease._locked(path, blocking=False):
                        current = lease._read(path)
                        deadline = lease._deadline(current)
                        if current.holder == lease.HUMAN and (deadline is None or deadline > time.time()):
                            return False
                        if not self._expired(path):
                            return False
                        replicas, version = self._scale()
                        if replicas:
                            self._set_scale(0, version)
                        self.activity.unlink(missing_ok=True)
                        return bool(replicas)
        except (BlockingIOError, FileNotFoundError):
            return False

    def _expired(self, lease_path: Path) -> bool:
        last_use = self.activity.stat().st_mtime
        for path in (self.home / "bot-desktop" / "activity", lease_path):
            try:
                last_use = max(last_use, path.stat().st_mtime)
            except FileNotFoundError:
                pass
        return time.time() - last_use >= self.idle_seconds


def stop_profile_sandbox_if_idle() -> None:
    """Called once per served profile by existing gateway housekeeping."""
    from hermes_constants import get_hermes_home
    from hermes_platform.resolver import locate_command
    from tools.terminal_tool import _get_env_config

    home = get_hermes_home()
    if not (home / "kubernetes-sandbox" / "activity").exists():
        return
    config = _get_env_config()
    if config["env_type"] != "kubernetes" or not config["kubernetes_idle_seconds"]:
        return
    executable = locate_command("kubectl").command
    if executable:
        SandboxLifecycle(home, executable[0], config["kubernetes_namespace"],
                         config["kubernetes_idle_seconds"]).stop_if_idle()


def main() -> int:
    """Run an operator-generated kubectl exec argv under a cross-process activity lease."""
    home, namespace, idle, mode, expected_uid, executable, *arguments = sys.argv[1:]
    lifecycle = SandboxLifecycle(Path(home), executable, namespace, int(idle))
    try:
        if mode == "passive":
            if lifecycle.identity()["uid"] != expected_uid:
                raise EnvironmentConnectionError("The Kubernetes sandbox changed before the probe")
            os.execv(executable, [executable, *arguments])
        with lifecycle.active(expected_uid=expected_uid or None, wake=not expected_uid) as (_, fd):
            # The exec client inherits the lease, so killing a wrapper can't retire
            # the Pod while that client is still running.
            return subprocess.run(  # health: allow HX006 -- the terminal or stream caller owns the command deadline
                [executable, *arguments], pass_fds=(fd,), check=False).returncode
    except (EnvironmentConnectionError, OSError) as error:
        print(str(error) if isinstance(error, EnvironmentConnectionError) else "Kubernetes sandbox exec failed",
              file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
