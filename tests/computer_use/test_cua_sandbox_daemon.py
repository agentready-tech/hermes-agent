"""Unrestricted and bounded computer-use daemons obey terminal placement, not the host PM pin."""
import json
from types import SimpleNamespace

import pytest

from tools.computer_use import cua_backend, cua_backend_daemon as daemon_module
from tools.computer_use import cua_backend_sandbox as sandbox
from tools.computer_use import cua_backend_session as sessions


class Transport:
    def __init__(self):
        self.prepared, self.closed = [], 0

    def prepare(self, manifest):
        self.prepared.append(manifest)
        return "/tmp/private/driver.sock", "/tmp/private/manifest.json" if manifest else None

    def client_env(self):
        return {"PATH": "/usr/bin"}

    def invocation(self, args):
        return ["sandbox-exec", *args]

    def close(self):
        self.closed += 1


@pytest.fixture
def remote(monkeypatch):
    transport, commands = Transport(), []
    monkeypatch.setattr(sandbox, "sandbox_daemon_transport", lambda mode: transport)
    monkeypatch.setattr(daemon_module._driver, "resolve_cua_driver_cmd", lambda: None)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", lambda cmd, **kw:
                        commands.append(cmd) or SimpleNamespace(stderr=(), poll=lambda: None, wait=lambda **kw: 0))
    monkeypatch.setattr(cua_backend, "_run_quiet", lambda cmd, **kw:
                        commands.append(cmd) or SimpleNamespace(returncode=0))
    return transport, commands


def test_unrestricted_uses_installed_sandbox_driver_without_host_pm(remote):
    transport, commands = remote
    daemon = daemon_module._EmbeddedCuaDaemon("", "unrestricted")
    daemon.start()
    proxy, args = daemon.proxy_invocation()
    assert commands[0][:3] == ["sandbox-exec", "cua-driver", "serve"]
    assert "--dangerously-bypass-approvals" in commands[0]
    assert commands[1][:3] == ["sandbox-exec", "cua-driver", "status"]
    assert proxy == "sandbox-exec" and args[:2] == ["cua-driver", "mcp"]
    assert daemon.child_env() == {"PATH": "/usr/bin"}
    daemon.stop()
    assert commands[-1][:3] == ["sandbox-exec", "cua-driver", "stop"]
    assert transport.closed == 1


def test_bounded_copies_original_manifest_on_each_lifecycle(remote, tmp_path):
    transport, commands = remote
    manifest = tmp_path / "ceiling.json"
    manifest.write_text('{"version":3}')
    daemon = daemon_module._EmbeddedCuaDaemon("", "bounded", str(manifest))
    daemon.start()
    assert commands[0][commands[0].index("--capability-manifest") + 1] == "/tmp/private/manifest.json"
    daemon.stop()
    daemon.start()
    assert transport.prepared == [str(manifest), str(manifest)]
    daemon.stop()


def test_remote_cli_recovery_never_resolves_or_writes_host_driver_files(remote, monkeypatch):
    transport, _commands = remote
    daemon = daemon_module._EmbeddedCuaDaemon("", "unrestricted")
    daemon._sandbox_transport = transport
    daemon._command = "cua-driver"
    session = sessions._CuaDriverSession(SimpleNamespace(), daemon)
    observed = []
    monkeypatch.setattr(sessions, "_cli_run_json", lambda cmd, env, name, timeout:
                        observed.append(cmd) or {"ok": True})
    session._call_tool_via_cli("get_window_state", {"window_id": "synthetic"}, 2)
    assert observed[0][:4] == ["sandbox-exec", "cua-driver", "call", "get_window_state"]
    assert "screenshot_out_file" not in json.loads(observed[0][4])


def test_refused_sandbox_never_tries_host_binary(monkeypatch):
    from tools.bot_desktop import runtime
    def refuse():
        raise RuntimeError("placement refused")
    monkeypatch.setattr(runtime, "tool_placement", refuse)
    with pytest.raises(RuntimeError, match="placement refused"):
        sandbox.sandbox_daemon_transport("unrestricted")


def test_manifest_bytes_and_permissions_are_preserved(monkeypatch, tmp_path):
    from tools.environments import streams
    data = b'{"version":3,"ceiling":"synthetic"}'
    manifest = tmp_path / "source.json"
    manifest.write_bytes(data)
    calls = []
    monkeypatch.setattr(streams, "run_in", lambda env, argv, **kw:
                        calls.append((argv, kw)) or SimpleNamespace(returncode=0))
    transport = sandbox._SandboxDaemonTransport(object(), {"DISPLAY": ":22"}, "pn", "bounded")
    socket, copied = transport.prepare(str(manifest))
    assert calls[0][1]["stdin"] == data and calls[0][1]["user"] == "pn"
    assert "mode=0o700" in calls[0][0][2] and "0o600" in calls[0][0][2]
    assert copied.startswith(transport.directory) and socket.startswith(transport.directory)
    assert "DANGEROUSLY" not in str(transport.published)
