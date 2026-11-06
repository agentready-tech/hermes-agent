"""The transport never falls back to the gateway or places secrets on argv."""

import json
from types import SimpleNamespace

import pytest

from hermes_runtime.workspace.store import WorkspaceError
from hermes_runtime.workspace.transport import sandbox_request


def home(tmp_path):
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: local\n")
    return tmp_path


def test_missing_or_local_sandbox_refuses_before_any_host_process(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: None)
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("No host fallback is permitted"))
    with pytest.raises(WorkspaceError, match="SANDBOX_UNAVAILABLE"):
        sandbox_request(home(tmp_path), "sandbox_mail", {"operation": "search"})


def test_sandbox_receives_credentials_only_on_protected_stdin(monkeypatch, tmp_path):
    env = object()
    seen = []
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: env)
    monkeypatch.setattr("tools.environments.streams.remote_command", lambda selected, argv, **kwargs: ["synthetic-native-exec", *argv] if selected is env else None)
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
