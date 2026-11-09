"""Successful own-account Google reconnects replace grants, never identities or user state."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_runtime.workspace import control, sandbox_google
from hermes_runtime.workspace.sandbox_mail import MailError
from hermes_runtime.workspace.service import Workspace
from hermes_runtime.workspace.store import SERVICES, WorkspaceError

ENV = "env_" + "1" * 32
FIRST = "connection_" + "2" * 32
DUPLICATE = "connection_" + "3" * 32
NEW = "connection_" + "4" * 32
GOOGLE = {"clientId": "fixture-client", "clientSecret": "synthetic-client-secret"}


def metadata(identity=FIRST, **changes):
    return {"id": identity, "environmentId": ENV, "provider": "google", "name": "My Google account",
        "address": "person@example.invalid", "services": ["mailRead"], "proactive": False,
        "senderPolicy": "allowlist", "allowedSenders": [], "since": "2026-10-01T00:00:00Z",
        "createdAt": "2026-10-01T00:00:00Z", "gmailHistoryId": "41", "status": "connected", "enabled": True, **changes}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "config.yaml").write_text(f"autonomous:\n  workspace:\n    native: true\n    environment_id: {ENV}\n")
    return Workspace(tmp_path)


def seed(workspace, *connections):
    with workspace.store.transaction() as state:
        state["google"] = dict(GOOGLE)
        state["connections"] = {row["id"]: {"connection": copy.deepcopy(row), "credential": {"refreshToken": "synthetic-old-" + row["id"]}} for row in connections}


def request(identity=NEW):
    return {"operation": "google_exchange", "connectionId": identity, "environmentId": ENV,
        "code": "synthetic-code", "verifier": "synthetic-verifier", "redirectUri": "https://panel.invalid/app/api/workspace/google/callback",
        **GOOGLE, "services": sorted(SERVICES)}


def grant(address="PERSON@example.invalid"):
    return {"address": address, "credential": {"refreshToken": "synthetic-fresh"}, "gmailHistoryId": "99"}


def exchange(workspace, value=None):
    return control.dispatch(workspace.home, value or request(), workspace=workspace)["connection"]


def test_successful_reconnect_keeps_canonical_id_rules_history_and_encrypted_cursor_state(workspace):
    original = metadata(proactive=True, senderPolicy="all", allowedSenders=["lead@example.invalid"], lastCheckedAt="2026-11-01T00:00:00Z")
    other = metadata("connection_" + "5" * 32, address="other@example.invalid")
    seed(workspace, original, metadata(DUPLICATE, createdAt="2026-11-01T00:00:00Z"), other)
    cursor = workspace.home / "autonomous" / "workspace-cursors.json"
    cursor.parent.mkdir(); cursor.write_text(json.dumps({FIRST: {"historyId": "72"}, DUPLICATE: {"historyId": "75"}}))
    before_cursor = cursor.read_bytes()
    workspace.runner = lambda *_: grant()
    result = exchange(workspace)
    assert result["id"] == FIRST and set(result["services"]) == SERVICES
    for key in ("name", "since", "createdAt", "gmailHistoryId", "proactive", "senderPolicy", "allowedSenders", "lastCheckedAt"):
        assert result[key] == original[key]
    assert set(workspace.store.read()["connections"]) == {FIRST, other["id"]}
    assert workspace.store.get(FIRST)["credential"] == grant()["credential"]
    assert workspace.store.get(other["id"])["credential"]["refreshToken"].startswith("synthetic-old-")
    assert cursor.read_bytes() == before_cursor
    assert "credential" not in result and b"synthetic-fresh" not in workspace.store.vault._vault_path.read_bytes()


def test_custom_duplicate_rules_move_to_default_canonical_without_broadening(workspace):
    custom = metadata(DUPLICATE, createdAt="2026-11-01T00:00:00Z", proactive=True, allowedSenders=["a@example.invalid", "a@example.invalid"])
    seed(workspace, metadata(), custom)
    workspace.runner = lambda *_: grant()
    result = exchange(workspace)
    assert result["id"] == FIRST and result["proactive"] is True
    assert result["senderPolicy"] == "allowlist" and result["allowedSenders"] == custom["allowedSenders"]
    assert result["since"] == metadata()["since"] and result["gmailHistoryId"] == "41"


def test_conflicting_nondefault_rules_refuse_merge_without_changing_vault(workspace):
    seed(workspace, metadata(proactive=True, allowedSenders=["a@example.invalid"]),
         metadata(DUPLICATE, proactive=True, senderPolicy="all"))
    before = workspace.store.vault._vault_path.read_bytes()
    workspace.runner = lambda *_: grant()
    with pytest.raises(WorkspaceError, match="INVALID_GOOGLE_DUPLICATE_RULES_CONFLICT"):
        exchange(workspace)
    assert workspace.store.vault._vault_path.read_bytes() == before


def test_reconnect_at_connection_cap_succeeds_but_new_account_preserves_old_records(workspace):
    rows = [metadata("connection_" + str(index) * 32, address=f"other{index}@example.invalid") for index in range(5, 9)]
    seed(workspace, metadata(), *rows)
    workspace.runner = lambda *_: grant()
    assert exchange(workspace)["id"] == FIRST
    before = workspace.store.vault._vault_path.read_bytes()
    workspace.runner = lambda *_: grant("new@example.invalid")
    with pytest.raises(WorkspaceError, match="CONNECTION_LIMIT"):
        exchange(workspace)
    assert workspace.store.vault._vault_path.read_bytes() == before


def test_concurrent_reconnects_commit_one_canonical_identity(workspace):
    barrier = threading.Barrier(2)
    def run(*_):
        barrier.wait(timeout=5)
        return grant()
    workspace.runner = run
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(exchange, workspace, request(FIRST))
        two = pool.submit(exchange, workspace, request(DUPLICATE))
        assert one.result(timeout=10)["id"] == two.result(timeout=10)["id"]
    assert len(workspace.store.list()) == 1


def test_partial_requested_services_refused_before_exchange(workspace):
    workspace.runner = lambda *_: pytest.fail("Partial request must not reach Google")
    value = request(); value["services"] = ["mailRead"]
    with pytest.raises(WorkspaceError, match="INVALID_WORKSPACE_PERMISSIONS"):
        exchange(workspace, value)


def test_incomplete_actual_grant_leaves_existing_connection_and_credentials_untouched(workspace, monkeypatch):
    seed(workspace, metadata())
    before = workspace.store.vault._vault_path.read_bytes(); calls = []
    def http(url, *args, **kwargs):
        calls.append(url)
        return {"refresh_token": "synthetic-incomplete", "access_token": "synthetic-access", "scope": sandbox_google.SCOPES["mailRead"]}
    monkeypatch.setattr(sandbox_google, "json_http", http)
    def run(_home, _program, payload):
        try:
            return sandbox_google.execute_google(payload)
        except MailError as error:
            raise WorkspaceError(str(error)) from None
    workspace.runner = run
    with pytest.raises(WorkspaceError, match="PERMISSIONS_NOT_GRANTED"):
        exchange(workspace)
    assert len(calls) == 1 and workspace.store.vault._vault_path.read_bytes() == before


def test_cross_provider_id_collision_and_foreign_account_scope_never_merge(workspace):
    imap = metadata(provider="imap_smtp", services=["mailRead"])
    seed(workspace, imap)
    workspace.runner = lambda *_: pytest.fail("Known provider collision must precede exchange")
    with pytest.raises(WorkspaceError, match="FORBIDDEN"):
        exchange(workspace, request(FIRST))
    foreign = metadata(environmentId="env_" + "9" * 32)
    seed(workspace, foreign); before = workspace.store.vault._vault_path.read_bytes()
    workspace.runner = lambda *_: grant()
    with pytest.raises(WorkspaceError, match="FORBIDDEN"):
        exchange(workspace)
    assert workspace.store.vault._vault_path.read_bytes() == before


def test_client_change_during_exchange_refuses_stale_application_overwrite(workspace):
    seed(workspace, metadata())
    def run(*_):
        with workspace.store.transaction() as state:
            state["google"] = {"clientId": "another-client", "clientSecret": "synthetic-other"}
        return grant()
    workspace.runner = run
    with pytest.raises(WorkspaceError, match="INVALID_GOOGLE_CLIENT_IN_USE"):
        exchange(workspace)
    assert workspace.store.read()["google"]["clientId"] == "another-client"
    assert workspace.store.get(FIRST)["credential"]["refreshToken"].startswith("synthetic-old-")
