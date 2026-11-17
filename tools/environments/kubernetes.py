"""Execute in one operator-managed Kubernetes sandbox, optionally starting it on demand."""

import json
import logging
import re
import subprocess
import sys
import uuid

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.base_output import _popen_bash
from tools.environments.kubernetes_process import CANCEL, RUN, frame
from tools.environments.remote_common import resolve_passthrough_env, run_capture
from hermes_platform.resolver import locate_command

logger = logging.getLogger(__name__)
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")


def validate_target(namespace: str, pod: str, container: str) -> None:
    for label, value, limit in (("namespace", namespace, 63), ("pod", pod, 253), ("container", container, 63)):
        parts = value.split(".") if label == "pod" else [value]
        if not value or len(value) > limit or any(len(p) > 63 or not _LABEL.fullmatch(p) for p in parts):
            raise ValueError(f"Set a valid terminal.kubernetes_{label}; an explicit sandbox target is required")


def read_pod(executable: str, namespace: str, pod: str, *, missing_ok: bool = False) -> dict:
    args = [executable, "--namespace", namespace, "get", "pod", pod, "-o", "json"]
    if missing_ok:
        args.append("--ignore-not-found")
    try:
        result = run_capture(args, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise EnvironmentConnectionError("Cannot reach the configured Kubernetes sandbox") from error
    if result.returncode:
        # Auth plugins and API errors can contain credentials; never include their raw output.
        raise EnvironmentConnectionError("Kubernetes sandbox is unavailable or access was denied",
            retry_hint="Check the configured namespace/Pod, its ServiceAccount permissions and API connection.")
    try:
        if missing_ok and not result.stdout.strip():
            return {}
        return json.loads(result.stdout)
    except ValueError as error:
        raise EnvironmentConnectionError("Kubernetes returned invalid sandbox metadata") from error


def pod_identity(value: dict, namespace: str, pod: str, container: str) -> dict:
    metadata, status = value.get("metadata", {}), value.get("status", {})
    target = next((c for c in status.get("containerStatuses", []) if c.get("name") == container), {})
    if (metadata.get("namespace") != namespace or metadata.get("name") != pod or not metadata.get("uid")
            or metadata.get("deletionTimestamp") or status.get("phase") != "Running"
            or not target.get("state", {}).get("running")
            or not target.get("containerID")):
        raise EnvironmentConnectionError("The configured Kubernetes sandbox container is not running",
            retry_hint="Start or repair its workload through the platform; Hermes will not create a replacement Pod.")
    return {"namespace": namespace, "pod": pod, "container": container,
            "uid": metadata["uid"], "container_id": target["containerID"]}


def marker_alive(identity: dict) -> bool:
    try:
        namespace, pod, container = (identity[k] for k in ("namespace", "pod", "container"))
        validate_target(namespace, pod, container)
        current = pod_identity(read_pod(identity["kubectl"], namespace, pod), namespace, pod, container)
        return all(current[k] == identity[k] for k in current)
    except (OSError, ValueError, KeyError, EnvironmentConnectionError, subprocess.SubprocessError):
        return False


class KubernetesEnvironment(BaseEnvironment):
    """A persistent sandbox selected by namespace, stable Pod name and container name.

    kubectl owns API authentication (in-cluster ServiceAccount or operator KUBECONFIG).
    The platform owns Pod creation, volumes and security policy. Cleanup never deletes it.
    """

    _profile_scoped_passthrough = True
    _sudo_nopasswd_probe_supported = True
    chromium_sandbox_enabled = True

    def __init__(self, namespace: str, pod: str, container: str, cwd: str = "/workspace",
                 timeout: int = 180, persistent: bool = True, probe_only: bool = False,
                 idle_seconds: int = 0):
        validate_target(namespace, pod, container)
        command = locate_command("kubectl").command
        if not command:
            raise EnvironmentConnectionError("kubectl is required for the Kubernetes terminal backend")
        self._kubectl_exe = command[0]
        self.namespace, self.pod, self.container = namespace, pod, container
        self._persistent = True
        self._probe_only = probe_only
        self.lifecycle = None
        if idle_seconds:
            from hermes_constants import get_hermes_home
            from tools.environments.kubernetes_lifecycle import SandboxLifecycle

            if (pod, container) != ("sandbox-0", "sandbox"):
                raise ValueError("On-demand Kubernetes lifecycle requires the enrolled sandbox StatefulSet")
            self.lifecycle = SandboxLifecycle(get_hermes_home(), self._kubectl_exe, namespace, idle_seconds)
        super().__init__(cwd=cwd, timeout=timeout, persist_env=persistent)
        self.kubernetes_identity = {}
        if probe_only:
            self.kubernetes_identity = self._identity()
        else:
            self.activate()

    def activate(self) -> None:
        """A real use may wake an idle sandbox, even when an environment is cached."""
        if self.lifecycle:
            if self._probe_only:
                raise EnvironmentConnectionError("The probe cannot activate a Kubernetes sandbox")
            with self.lifecycle.active() as (identity, _):
                current = {**identity, "kubectl": self._kubectl_exe}
                self._refresh_identity(current)
        else:
            self._refresh_identity(self._identity())

    def _refresh_identity(self, current: dict) -> None:
        if current != self.kubernetes_identity:
            recreated = bool(self.kubernetes_identity)
            self.kubernetes_identity = current
            self._snapshot_ready = False
            self.init_session()
            if recreated:
                self._mark_recreated()

    def _identity(self) -> dict:
        if self.lifecycle:
            return {**self.lifecycle.identity(), "kubectl": self._kubectl_exe}
        value = read_pod(self._kubectl_exe, self.namespace, self.pod)
        return {**pod_identity(value, self.namespace, self.pod, self.container), "kubectl": self._kubectl_exe}

    def exec_prefix(self, *, user: str | None = None, interactive: bool = True,
                    passive: bool = False, require_current: bool = False, expected_uid: str = "") -> list[str]:
        argv = [self._kubectl_exe, "--namespace", self.namespace, "exec"]
        if interactive:
            argv.append("-i")
        argv.extend([self.pod, "--container", self.container, "--"])
        if user:
            argv.extend(["runuser", "-u", user, "--"])
        if self.lifecycle:
            # A probe-only handle stays tied to the same existing Pod.
            passive = passive or self._probe_only
            if (passive or require_current) and not expected_uid:
                expected_uid = self.kubernetes_identity["uid"]
            return [sys.executable, "-m", "tools.environments.kubernetes_lifecycle", str(self.lifecycle.home),
                    self.namespace, str(self.lifecycle.idle_seconds), "passive" if passive else "active",
                    expected_uid, *argv]
        return argv

    def _before_execute(self) -> None:
        self.activate()

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        values, unset = resolve_passthrough_env()
        record = f"{self.get_temp_dir()}/hermes-kube-{uuid.uuid4().hex}.json"
        payload = frame(cmd_string, record, values, unset, login, stdin_data)
        proc = _popen_bash(self.exec_prefix(require_current=True) + ["python3", "-c", RUN], payload)
        proc._hermes_kubernetes_record = record
        return proc

    def _kill_process(self, proc) -> None:
        record = getattr(proc, "_hermes_kubernetes_record", None)
        if record and proc.poll() is None:
            try:
                run_capture(self.exec_prefix(passive=True) + ["python3", "-c", CANCEL, record], timeout=8)
            except (OSError, subprocess.SubprocessError):
                logger.warning("Kubernetes command cancellation could not reach its sandbox")
        super()._kill_process(proc)

    def cleanup(self) -> None:
        """Release no platform-owned resources: a turn or idle reaper must not stop this Pod."""
