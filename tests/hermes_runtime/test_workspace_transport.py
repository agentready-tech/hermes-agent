"""The transport never falls back to the gateway or places secrets on argv."""

import json
import sys
from types import SimpleNamespace

import pytest

from hermes_runtime.workspace.store import WorkspaceError
from hermes_runtime.workspace.transport import poll_request, run_request, sandbox_request


def home(tmp_path):
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: local\n")
    return tmp_path


def test_missing_or_local_sandbox_refuses_before_any_host_process(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: None)
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("No host fallback is permitted"))
    with pytest.raises(WorkspaceError, match="SANDBOX_UNAVAILABLE"):
        sandbox_request(home(tmp_path), "sandbox_mail", {"operation": "search"})


@pytest.mark.parametrize("firecracker", [False, True])
def test_sandbox_receives_credentials_only_on_protected_stdin(monkeypatch, tmp_path, firecracker):
    from tools.environments.firecracker import FirecrackerEnvironment

    env = object.__new__(FirecrackerEnvironment) if firecracker else object()
    seen = []
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: env)
    def remote_command(selected, argv, *, user, interactive):
        assert selected is env and interactive
        if firecracker:
            assert user is None
            assert argv[:5] == ["/usr/sbin/runuser", "-u", "nobody", "--", "python3"]
        else:
            assert user == "nobody" and argv[0] == "python3"
        return ["synthetic-native-exec", *argv]

    monkeypatch.setattr("tools.environments.streams.remote_command", remote_command)
    monkeypatch.setattr("tools.environments.streams.client_environment", lambda: {"PATH": "/synthetic/client"})
    def execute(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true,"value":{"messages":[]}}')
    monkeypatch.setattr("subprocess.run", execute)
    secret = "synthetic-credential-never-argv"
    result = sandbox_request(home(tmp_path), "sandbox_mail", {"operation": "search", "credential": {"username": "synthetic", "password": secret}})
    assert result == {"messages": []}
    command, kwargs = seen[0]
    assert secret not in str(command) and secret not in str(kwargs["env"])
    assert json.loads(kwargs["input"])["credential"]["password"] == secret


def test_vendor_error_cannot_cross_transport_as_raw_private_text(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: object())
    monkeypatch.setattr("tools.environments.streams.remote_command", lambda *_args, **_kwargs: ["synthetic"])
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b'{"ok":false,"error":"private user and password response"}'))
    with pytest.raises(WorkspaceError, match="^INVALID_WORKSPACE_RESPONSE$"):
        sandbox_request(home(tmp_path), "sandbox_google", {"operation": "verify"})


@pytest.mark.parametrize(("diagnostic", "expected"), [
    ("api: HTTP 429", " (api: HTTP 429)"),
    ("oauth: timeout", " (oauth: timeout)"),
    ("api: HTTP 400 with private provider response", ""),
])
def test_mcp_shows_only_controlled_google_diagnostics(monkeypatch, diagnostic, expected):
    from hermes_runtime.workspace.mcp import dispatch

    code = "INVALID_GOOGLE_REQUEST_FAILED"
    output = json.dumps({"ok": False, "error": code, "diagnostic": diagnostic}).encode()
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output))
    workspace = SimpleNamespace(tool=lambda *_: run_request(["synthetic"], {}, "sandbox_google", {}, "UNAVAILABLE"))
    request = {"method": "tools/call", "params": {"name": "workspace_connections", "arguments": {}}}
    result = dispatch(request, workspace)
    assert result["isError"] is True
    assert result["content"][0]["text"] == code + expected


@pytest.mark.parametrize("request_args", [
    {"operation": "gws", "service": "sheets", "resource": ["spreadsheets", "values"], "method": "get"},
    {"operation": "gws", "service": "sheets", "resource": ["spreadsheets", "values"], "method": "batchGetByDataFilter"},
    {"operation": "request", "service": "sheets", "method": "POST", "path": "spreadsheets/fixture/values:batchGetByDataFilter"},
])
def test_lost_sheet_read_receipt_is_not_a_possible_write(monkeypatch, request_args):
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=125, stdout=b""))
    with pytest.raises(WorkspaceError, match="^INVALID_WORKSPACE_RESPONSE$"):
        run_request(["synthetic"], {}, "sandbox_google", request_args, "UNAVAILABLE")


@pytest.mark.parametrize("output,exit_code", [(b"not-json", 0), (b"[]", 0), (b"", 1), (b'{"ok":false,"error":"private provider text"}', 0)])
@pytest.mark.parametrize("program,payload,expected", [
    ("sandbox_mail", {"operation": "send"}, "INVALID_MAIL_SEND_OUTCOME_UNKNOWN"),
    ("sandbox_google", {"operation": "request", "method": "POST"}, "INVALID_GOOGLE_WRITE_OUTCOME_UNKNOWN"),
    ("sandbox_google", {"operation": "gws", "method": "update"}, "INVALID_GOOGLE_WRITE_OUTCOME_UNKNOWN"),
])
def test_lost_write_receipt_never_becomes_definite_failure(monkeypatch, tmp_path, output, exit_code, program, payload, expected):
    calls = []
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: object())
    monkeypatch.setattr("tools.environments.streams.remote_command", lambda *_args, **_kwargs: ["synthetic"])
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: calls.append(1) or SimpleNamespace(returncode=exit_code, stdout=output))
    with pytest.raises(WorkspaceError, match="^" + expected + "$"):
        sandbox_request(home(tmp_path), program, payload)
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="The Hermes collector uses Linux RLIMIT_AS")
@pytest.mark.parametrize("program,payload,expected", [
    ("sandbox_mail", {"operation": "incoming"}, "INVALID_MAIL_CONTENT"),
    ("sandbox_google", {"operation": "request", "service": "gmail", "method": "GET", "path": "profile",
                        "services": ["mailRead"]}, "INVALID_GOOGLE_RECONNECT_REQUIRED"),
])
def test_real_poll_child_reports_safe_missing_credentials(tmp_path, program, payload, expected):
    with pytest.raises(WorkspaceError, match="^" + expected + "$"):
        poll_request(tmp_path, program, payload)


def test_tool_search_still_requires_sandbox(monkeypatch, tmp_path):
    from hermes_runtime.workspace.service import Workspace
    from tests.hermes_runtime.test_workspace_native import ID, SECRET, connection

    workspace = Workspace(tmp_path)
    workspace.store.upsert(connection(), SECRET)
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: None)
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("Tool must use its sandbox"))
    with pytest.raises(WorkspaceError, match="^INVALID_MAIL_SANDBOX_UNAVAILABLE$"):
        workspace.search({"connectionId": ID})
