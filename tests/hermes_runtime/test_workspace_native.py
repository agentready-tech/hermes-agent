"""Native Workspace contracts with a real encrypted profile store and fake network."""

import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_runtime.workspace import control, mcp
from hermes_runtime.workspace.service import Workspace
from hermes_runtime.workspace.store import ConnectionStore, WorkspaceError

ENV = "env_" + "1" * 32
ID = "connection_" + "2" * 32
SECRET = {"username": "synthetic@example.invalid", "password": "synthetic-never-in-output"}


def connection(**changes):
    return {"id": ID, "environmentId": ENV, "provider": "imap_smtp", "name": "Synthetic", "address": "synthetic@example.invalid",
        "services": ["mailRead", "mailSend"], "inbound": {"host": "imap.example.invalid", "port": 993, "security": "tls"},
        "outbound": {"host": "smtp.example.invalid", "port": 587, "security": "starttls"}, "enabled": True,
        "proactive": False, "senderPolicy": "allowlist", "allowedSenders": [], "since": "2026-10-01T00:00:00Z",
        "createdAt": "2026-10-01T00:00:00Z", "status": "connected", "verifiedAt": "2026-10-01T00:00:00Z", **changes}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "config.yaml").write_text(f"autonomous:\n  workspace:\n    native: true\n    environment_id: {ENV}\n")
    return Workspace(tmp_path)


def seed(workspace, metadata=None):
    return control.dispatch(workspace.home, {"operation": "import", "connections": [{"connection": metadata or connection(), "credential": SECRET}]}, workspace=workspace)


def test_import_is_atomic_idempotent_encrypted_and_public_only(workspace):
    first = seed(workspace)
    before = (workspace.store.vault._vault_path).read_bytes()
    assert first["imported"] == 1
    assert seed(workspace)["imported"] == 0
    assert before == workspace.store.vault._vault_path.read_bytes()
    assert SECRET["password"].encode() not in before
    assert first["connections"][0]["credentialConfigured"] is True
    assert "credential" not in first["connections"][0]
    assert SECRET["password"] not in json.dumps(first)
    assert workspace.store.vault._key_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(WorkspaceError, match="IMPORT_CONFLICT"):
        seed(workspace, connection(senderPolicy="all"))
    assert workspace.store.list()[0]["senderPolicy"] == "allowlist"
    assert before == workspace.store.vault._vault_path.read_bytes()


def test_cross_employee_import_and_arbitrary_panel_tool_calls_are_refused(workspace):
    for request in [
        {"operation": "import", "connections": [{"connection": connection(environmentId="env_" + "3" * 32), "credential": SECRET}]},
        {"operation": "mail_read", "connectionId": ID, "messageId": "7:1"},
        {"operation": "list", "tool": "mail_send"},
    ]:
        result = control.response(workspace.home, request, workspace=workspace)
        assert result["ok"] is False
        assert SECRET["password"] not in json.dumps(result)
    assert workspace.store.list() == []


def test_mcp_real_dispatch_has_metadata_search_pagination_and_no_secret_arguments(workspace):
    seed(workspace)
    calls = []
    def run(home, program, request):
        calls.append(copy.deepcopy(request))
        return {"messages": [{"id": "7:27", "subject": "Synthetic October"}], "hasMore": True, "nextCursor": "opaque"}
    workspace.runner = run
    source = io.StringIO("\n".join(json.dumps(row) for row in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "mail_search", "arguments": {"connectionId": ID, "query": "invoice", "since": "2026-10-27", "before": "2026-10-28", "cursor": "older", "limit": 10}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mail_search", "arguments": {"connectionId": ID, "password": "forbidden"}}},
    ]) + "\n")
    output = io.StringIO()
    mcp.serve(source, output, workspace)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[1]["result"]["isError"] is False
    assert json.loads(replies[1]["result"]["content"][0]["text"])["nextCursor"] == "opaque"
    assert calls[0]["since"] == "2026-10-27" and calls[0]["cursor"] == "older"
    assert calls[0]["credential"] == SECRET
    assert len(calls) == 1 and replies[2]["result"]["isError"] is True
    assert SECRET["password"] not in output.getvalue()


def test_permission_denial_precedes_network_and_rules_preserve_credentials(workspace):
    seed(workspace, connection(services=["mailRead"]))
    workspace.runner = lambda *_: pytest.fail("A denied grant must not reach the sandbox")
    with pytest.raises(WorkspaceError, match="FORBIDDEN"):
        workspace.send({"connectionId": ID, "to": "someone@example.invalid", "text": "synthetic"})
    result = control.dispatch(workspace.home, {"operation": "rules", "connectionId": ID, "proactive": True, "senderPolicy": "all", "allowedSenders": []}, workspace=workspace)
    assert result["connection"]["proactive"] is True
    assert workspace.store.get(ID)["credential"] == SECRET


def test_verify_is_native_and_updates_only_own_public_status(workspace):
    seed(workspace)
    seen = []
    workspace.runner = lambda home, program, request: seen.append((program, request["operation"])) or {"verified": True}
    result = control.dispatch(workspace.home, {"operation": "verify", "connectionId": ID}, workspace=workspace)
    assert result["verified"] is True and seen == [("sandbox_mail", "verify")]
    assert workspace.store.get(ID)["credential"] == SECRET


def test_profiles_do_not_share_connections_or_crypto(tmp_path):
    one, two = ConnectionStore(tmp_path / "one"), ConnectionStore(tmp_path / "two")
    one.upsert(connection(), SECRET, environment_id=ENV)
    assert two.list() == []
    with pytest.raises(WorkspaceError, match="NOT_FOUND"):
        two.get(ID)


def test_actual_control_cli_accepts_trusted_worker_envelope_and_rejects_cross_employee(workspace):
    for environment, expected in [(ENV, True), ("env_" + "9" * 32, False)]:
        result = subprocess.run([sys.executable, "-m", "hermes_runtime.workspace.control", "--home", str(workspace.home)],
            input=json.dumps({"operation": "list", "environmentId": environment}), text=True, capture_output=True, check=True)
        value = json.loads(result.stdout)
        assert value["ok"] is expected
        assert result.stderr == ""
