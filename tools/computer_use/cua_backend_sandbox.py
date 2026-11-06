"""Private cua-driver daemons use the same sandbox and display as ordinary MCP sessions."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any


class _SandboxDaemonTransport:
    def __init__(self, env: Any, published: dict[str, str], user: str, permission_mode: str) -> None:
        self.env, self.user = env, user
        self.published = {**published, "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
                          "CUA_DRIVER_PERMISSION_MODE": permission_mode}
        if permission_mode == "unrestricted":
            self.published["CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS"] = "1"
        # no-tmp: ok — sandbox-side private IPC directory; the gateway scratch path is not mounted there.
        self.directory = "/tmp/hermes-cua-" + uuid.uuid4().hex

    def client_env(self) -> dict[str, str]:
        from tools.environments import streams
        return streams.client_environment()

    def invocation(self, argv: list[str]) -> list[str]:
        from tools.environments import streams
        command = streams.remote_command(self.env, argv, child_env=self.published,
                                         user=self.user, interactive=True)
        if command is None:
            raise RuntimeError("the selected sandbox cannot host a private cua-driver daemon")
        return command

    def prepare(self, manifest: str | None) -> tuple[str, str | None]:
        """Copy only the reviewed capability ceiling into an owned, mode-0700 sandbox directory."""
        from tools.environments import streams
        data = Path(manifest).read_bytes() if manifest else b""
        script = ("import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.mkdir(mode=0o700); "
                  "data=sys.stdin.buffer.read(); "
                  "(p/'manifest.json').write_bytes(data) if data else None; "
                  "os.chmod(p/'manifest.json',0o600) if data else None")
        result = streams.run_in(self.env, ["python3", "-c", script, self.directory],
                                user=self.user, timeout=10, stdin=data)
        if result.returncode:
            raise RuntimeError("could not prepare the private cua-driver sandbox directory")
        return self.directory + "/driver.sock", self.directory + "/manifest.json" if manifest else None

    def close(self) -> None:
        from tools.environments import streams
        script = "import shutil,sys; shutil.rmtree(sys.argv[1],ignore_errors=True)"
        streams.run_in(self.env, ["python3", "-c", script, self.directory], user=self.user, timeout=10)


def sandbox_daemon_transport(permission_mode: str) -> _SandboxDaemonTransport | None:
    """Placement is authoritative; a stopped or refused sandbox never falls back to the gateway."""
    from tools.bot_desktop import placement, runtime, sandbox_host
    if runtime.tool_placement() == placement.GATEWAY:
        return None
    published = runtime.published_env()
    if not published.get("DISPLAY"):
        raise RuntimeError("the screen inside the terminal backend's sandbox is gone; start it again")
    env = runtime._sandbox_env(create=True)
    if env is None:
        raise RuntimeError("the terminal backend's sandbox is not running")
    runtime.touch_activity()
    return _SandboxDaemonTransport(env, published, sandbox_host._user_for(env), permission_mode)
