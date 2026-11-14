"""Firecracker terminal backend using the worker's profile-scoped Unix socket."""

from contextlib import contextmanager
import logging
import re
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.base_output import _popen_bash
from tools.environments.kubernetes_process import CANCEL, RUN, frame
from tools.environments.remote_common import resolve_passthrough_env, run_capture

logger = logging.getLogger(__name__)
SOCKET_ROOT = Path("/run/agentsee-firecracker")
CLIENT = Path(__file__).with_name("firecracker_client.py")
GENERATION = re.compile(r"[a-f0-9]{32}\Z")
GUEST_EXEC_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def profile_socket() -> Path:
    home = get_hermes_home().resolve()
    if home.parent != Path("/root/.hermes/profiles") or not re.fullmatch(
        r"mike|env_[a-f0-9]{32}", home.name
    ):
        raise EnvironmentConnectionError("Firecracker requires an enrolled managed profile")
    return SOCKET_ROOT / f"{home.name}.sock"


def _request(path: Path, operation: str, *, timeout: float = 2, **payload) -> dict:
    from tools.environments.firecracker_client import exchange

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            return exchange(connection, {"operation": operation, **payload})
    except (OSError, EOFError, ValueError) as error:
        raise EnvironmentConnectionError("The profile's Firecracker worker is unavailable") from error


def generation(path: Path, *, wake: bool) -> str | None:
    state = _request(path, "ensure" if wake else "status", timeout=25 if wake else 2)
    try:
        value = state["generation"]
        if value is None and not wake:
            return None
        if not isinstance(value, str) or not GENERATION.fullmatch(value):
            raise ValueError
        return value
    except (ValueError, KeyError) as error:
        raise EnvironmentConnectionError("The profile's Firecracker worker is unavailable") from error


def watchdog_events() -> list[dict]:
    result = _request(profile_socket(), "watchdog-events").get("events")
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise EnvironmentConnectionError("Invalid Firecracker watchdog receipt response")
    return result


def acknowledge_watchdog_event(event_id: str) -> None:
    _request(profile_socket(), "watchdog-ack", event_id=event_id)


def marker_alive(identity: dict) -> bool:
    try:
        current = generation(profile_socket(), wake=False)
        return current is not None and current == identity["generation"]
    except (EnvironmentConnectionError, KeyError):
        return False


@contextmanager
def browser_activity():
    """Keep the live browser generation available through a real tool response."""
    from tools.environments.firecracker_client import exchange

    path = profile_socket()
    current = generation(path, wake=False)
    if current is None:
        raise EnvironmentConnectionError("The Firecracker browser is no longer running")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.settimeout(3)
            connection.connect(str(path))
            reply = exchange(connection, {"operation": "activity", "generation": current})
            if reply.get("generation") != current:
                raise ValueError("Firecracker generation changed")
        except (OSError, EOFError, ValueError) as error:
            raise EnvironmentConnectionError("The Firecracker browser is no longer running") from error
        yield


class FirecrackerEnvironment(BaseEnvironment):
    _persistent = True
    _profile_scoped_passthrough = True
    _sudo_nopasswd_probe_supported = True
    chromium_sandbox_enabled = True

    def __init__(self, cwd: str, timeout: int, persistent: bool, probe_only: bool = False):
        self._socket = profile_socket()
        self._generation_lock = threading.RLock()
        self.instance_id = generation(self._socket, wake=not probe_only)
        super().__init__(cwd=cwd, timeout=timeout, persist_env=persistent)
        if not probe_only:
            self.init_session()

    @property
    def firecracker_identity(self) -> dict:
        return {"generation": self.instance_id}

    def exec_prefix(self, *, user: str | None = None, interactive: bool = True) -> list[str]:
        self._before_execute()
        # Direct exec inherits the initramfs PATH, which omits image tools and Node.
        return [*self._prefix(user=user, interactive=interactive), "/usr/bin/env", f"PATH={GUEST_EXEC_PATH}"]

    def probe_prefix(self, *, user: str | None = None, interactive: bool = False) -> list[str]:
        return [*self._prefix(user=user, interactive=interactive, operation="probe"),
                "/usr/bin/env", f"PATH={GUEST_EXEC_PATH}"]

    def relay_prefix(self, expected_generation: str, *, user: str | None = None) -> list[str]:
        return [*self._prefix(user=user, interactive=True, operation="relay", identity=expected_generation),
                "/usr/bin/env", f"PATH={GUEST_EXEC_PATH}"]

    def _prefix(self, *, user: str | None = None, interactive: bool = True,
                operation: str = "exec", identity: str | None = None) -> list[str]:
        argv = [sys.executable, str(CLIENT), str(self._socket), identity or self.instance_id or ""]
        if interactive:
            argv.append("--stdin")
        if user:
            argv.extend(["--user", user])
        if operation != "exec":
            argv.append(f"--{operation}")
        return [*argv, "--"]

    def _before_execute(self) -> None:
        with self._generation_lock:
            current = generation(self._socket, wake=True)
            if current != self.instance_id:
                self.instance_id = current
                self._snapshot_ready = False
                self.init_session()
                self._mark_recreated()

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        values, unset = resolve_passthrough_env()
        record = f"{self.get_temp_dir()}/hermes-fc-{uuid.uuid4().hex}.json"
        payload = frame(cmd_string, record, values, unset, login, stdin_data)
        with self._generation_lock:
            prefix = self._prefix()
            cancel = [*self._prefix(interactive=False), "python3", "-c", CANCEL, record]
        proc = _popen_bash(prefix + ["python3", "-c", RUN], payload)
        proc._hermes_firecracker_cancel = cancel
        return proc

    def _kill_process(self, proc) -> None:
        cancel = getattr(proc, "_hermes_firecracker_cancel", None)
        if cancel and proc.poll() is None:
            try:
                run_capture(cancel, timeout=8)
            except (OSError, subprocess.SubprocessError):
                logger.warning("Firecracker command cancellation could not reach its sandbox")
        super()._kill_process(proc)

    def cleanup(self) -> None:
        """The worker owns VM lifetime independently of a Hermes turn."""
