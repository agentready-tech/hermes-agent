"""Persistent children must return through the same owner route as ephemeral children."""
import json
from queue import Queue
import sqlite3
import threading

import pytest

from tests.tools.test_delegate_projects import _parent
from tests.tools.test_delegate_mail_live import _response
from tests.waiting import wait_until


@pytest.mark.parametrize("platform,key,raw", [
    ("telegram", "agent:main:telegram:dm:fixture-chat", ""),
    ("local", "agent:main:local:dm:main", ""),
    ("api_server", "api-client", "api-client"),
])
def test_saved_child_completion_keeps_owner_route(tmp_path, monkeypatch, platform, key, raw):
    from gateway.session_context import set_session_vars, clear_session_vars
    from gateway.session_finite import finite_turn_scope
    from run_agent import AIAgent
    from tools.async_delegation import get_durable_delegation
    from tools.delegate_projects import project_action
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\n")
    entered, release = threading.Event(), threading.Event()

    def model(self, payload, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=15)
        return _response("SAVED_CHILD_RESULT")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    tokens = set_session_vars(platform=platform, session_key=key, session_id="main",
        chat_id=raw or "fixture-chat", async_delivery=not raw, session_history_delivery="1" if raw else None)
    try:
        with finite_turn_scope(False):
            result = json.loads(registry.dispatch("delegate_task", {"tasks": [
                {"goal": "Check saved evidence", "project": "routing-proof"}]}, parent_agent=parent))
        assert entered.wait(timeout=5)
        identity = result["projects"][0]["message_id"]
        release.set()
        wait_until(lambda: (get_durable_delegation(identity) or {}).get("state") == "completed", timeout=15)
        with sqlite3.connect(tmp_path / "state.db") as conn:
            event = json.loads(conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id=?",
                                           (identity,)).fetchone()[0])
        assert event["session_key"] == key
        assert event["origin_session_id"] == raw
        assert event["parent_session_id"] == parent.session_id
        assert event["summary"] == "SAVED_CHILD_RESULT"
        # Completed project repair must preserve the same route too: no second child/model run.
        from tools.delegate_projects import _publish_completed
        from tools.delegate_project_store import ProjectStore
        with sqlite3.connect(tmp_path / "state.db") as conn:
            conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (identity,))
        with ProjectStore(parent) as store:
            project = store.get(result["projects"][0]["subagent_id"])
            receipt = store.message(identity)
            _publish_completed(store, project, receipt, json.loads(receipt["result_json"]))
        repaired = get_durable_delegation(identity)
        assert repaired["origin_session"] == key
        assert repaired["origin_session_id"] == raw
        assert repaired["result"]["summary"] == "SAVED_CHILD_RESULT"
    finally:
        release.set()
        clear_session_vars(tokens)
        if entered.is_set():
            wait_until(lambda: not any(p["status"] == "running" for p in project_action(parent, "list")["projects"]), timeout=15)
        parent.close()
        db.close()


@pytest.mark.parametrize("case", ["owner", "foreign_scope", "api", "ambiguous", "closed"])
def test_restart_recovers_only_proven_legacy_project_route(tmp_path, monkeypatch, case):
    from gateway.session import SessionEntry, SessionSource
    from gateway.config import Platform
    from datetime import datetime
    from tools import async_delegation as delegation
    from tools.delegate_project_store import ProjectStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent, db = _parent(tmp_path)
    origin = {"wake": True, "session_key": "api-main" if case == "api" else ""}
    with ProjectStore(parent) as store:
        project = store.create("legacy", "Saved work", "")
        identity = store.enqueue(project, "Saved work", origin)
        message = store.claim(project)
        store.settle(project, message, {"status": "completed", "summary": "EXISTING_RESULT"})
    event = {"type": "async_delegation", "delegation_id": identity, "session_key": "main",
             "origin_session_id": "main", "parent_session_id": "main", "status": "completed",
             "dispatched_at": message["accepted_at"], "summary": "EXISTING_RESULT"}
    delegation._persist_dispatch(event)
    delegation._persist_completion(event, {"summary": "EXISTING_RESULT"})
    source = SessionSource(platform=Platform.LOCAL, chat_id="main")
    now = datetime.now()
    keys = ["agent:main:local:dm:main"] + (["agent:main:local:dm:other"] if case == "ambiguous" else [])
    scope = str((tmp_path / ("foreign" if case == "foreign_scope" else "sessions")).resolve())
    for key in keys:
        entry = SessionEntry(key, "main", now, now, origin=source, suspended=case == "closed")
        db.save_gateway_routing_entry(key, json.dumps(entry.to_dict()), scope=scope)
    queue = Queue()
    try:
        assert delegation.restore_undelivered_completions(queue) == 1
        restored = queue.get_nowait()
        row = delegation.get_durable_delegation(identity)
        if case == "owner":
            assert restored["session_key"] == keys[0]
            assert restored["origin_session_id"] == ""
            assert row["origin_session"] == keys[0]
            assert row["origin_session_id"] == ""
        else:
            assert restored["session_key"] == "main"
            assert row["origin_session"] == "main"
        assert row["delivery_state"] == "pending" and row["delivery_attempts"] == 0
        assert row["result"]["summary"] == "EXISTING_RESULT"
        assert restored["delegation_id"] == identity
    finally:
        parent.close()
        db.close()
