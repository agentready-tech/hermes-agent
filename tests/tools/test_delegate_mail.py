"""Addressed delegation mail is durable and does not finish the sending task."""

import json
import sqlite3

import pytest

from tests.tools.test_delegate_projects import _parent


@pytest.fixture
def family(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools.delegate_project_store import ProjectStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\n")
    parent, db = _parent(tmp_path)
    children = []
    with ProjectStore(parent) as store:
        projects = [store.create(key, f"Investigate {key}", None) for key in ("supply", "delivery")]
    for p in projects:
        db.create_session(p["session_id"], source="cli", parent_session_id="main")
        child = AIAgent(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1",
            api_key="test-only", api_mode="chat_completions", session_id=p["session_id"], session_db=db,
            parent_session_id="main", enabled_toolsets=["delegation"], skip_context_files=True,
            load_soul_identity=False, skip_memory=True, quiet_mode=True)
        child._delegate_depth, child._delegate_role = 2, "leaf"
        children.append(child)
    yield tmp_path, parent, children, db
    for child in children:
        child.close()
    parent.close()
    db.close()


def call(agent, action, **kwargs):
    from tools.registry import registry
    result = json.loads(registry.dispatch("delegate_task", {"action": action, **kwargs}, parent_agent=agent))
    assert not result.get("error"), result
    return result


def test_leaf_can_address_parent_and_sibling_without_finishing(family):
    home, parent, (supply, delivery), _ = family
    peers = call(supply, "peers")
    assert peers["parent_id"] == parent.session_id
    assert delivery.session_id in {item["subagent_id"] for item in peers["agents"]}
    question = call(supply, "message", subagent_id="parent", message="Which deadline should I use?", event_id="question-1")
    repeated = call(supply, "message", subagent_id="parent", message="Which deadline should I use?", event_id="question-1")
    assert question["message_id"] == repeated["message_id"]
    call(delivery, "message", subagent_id=supply.session_id, message="Supplier confirmed Monday")
    assert call(parent, "wait", timeout_seconds=0)["status"] == "messages_pending"
    assert call(supply, "wait", timeout_seconds=0)["status"] == "messages_pending"
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegation_mail").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM delegated_project_messages").fetchone()[0] == 0


def test_mail_does_not_grant_spawn_or_control_over_siblings(family):
    from tools.registry import registry
    from tools.delegate_mail_store import MailStore

    _, _parent_agent, (supply, delivery), db = family
    spawned = json.loads(registry.dispatch("delegate_task", {"goal": "Create another worker"}, parent_agent=supply))
    assert spawned.get("error")
    denied = json.loads(registry.dispatch("delegate_task", {"action": "read", "subagent_id": delivery.session_id}, parent_agent=supply))
    assert denied.get("error")
    db.create_session("unrelated", source="cli")
    with MailStore(supply) as store:
        with pytest.raises(ValueError, match="delegation tree"):
            store.send("unrelated", "Wrong conversation")
    call(supply, "message", subagent_id="parent", message="Original evidence", event_id="evidence")
    with MailStore(supply) as store:
        with pytest.raises(ValueError, match="different"):
            store.send("parent", "Conflicting evidence", "evidence")


def test_mail_respects_compression_lineage_and_profile_scope(family, monkeypatch, tmp_path):
    from tools.delegate_mail_store import MailStore
    from tools.registry import registry

    _, parent, (supply, _), db = family
    db.append_message(parent.session_id, "user", "Original history")
    call(parent, "message", subagent_id=supply.session_id, message="Question from the original parent ID")
    queued = call(supply, "message", subagent_id="parent", message="Keep this through compression")
    db.end_session(parent.session_id, "compression")
    db.create_session("main-compacted", source="cli", parent_session_id=parent.session_id)
    db.append_message("main-compacted", "user", "Compressed history")
    parent.session_id = "main-compacted"
    with MailStore(parent) as store:
        assert store.pending()[0]["message_id"] == queued["message_id"]
    assert call(supply, "peers")["parent_id"] == parent.session_id
    call(supply, "message", subagent_id="main", message="Reply addressed to the pre-compression sender")
    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    refused = json.loads(registry.dispatch("delegate_task", {"action": "message", "subagent_id": "parent", "message": "Do not leak"}, parent_agent=supply))
    assert refused.get("error")
    assert not (other / "state.db").exists()


def test_coordination_does_not_consume_the_spawn_budget():
    from agent.tool_guardrails import ToolCallGuardrailController, ToolCallGuardrailConfig, LoopCapConfig

    guard = ToolCallGuardrailController(ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_subagents=1)))
    assert guard.before_call("delegate_task", {"tasks": [{"goal": "One child"}]}).action == "allow"
    for _ in range(60):
        for action in ("peers", "message", "wait", "send", "read"):
            assert guard.before_call("delegate_task", {"action": action}).action == "allow"
    assert guard.before_call("delegate_task", {"goal": "Another child"}).action == "block"


def test_mail_delivery_wakeup_is_not_a_completion(family, monkeypatch):
    from tools import delegate_projects
    from tools.delegate_project_store import ProjectStore
    from tools.process_registry_notifications import format_process_notification, TimelineNotification
    from tools.async_delegation import restore_undelivered_completions
    import queue

    home, parent, (supply, delivery), _ = family
    with ProjectStore(parent) as store:
        # An older synchronous sibling must not steal this child's wakeup route.
        store.enqueue(store.get(supply.session_id), "Other original task", {"wake": False})
        p = store.get(delivery.session_id)
        store.enqueue(p, "Pending original task", {"wake": True, "session_key": "main-ui", "scope_id": "tenant-a"})
    receipt = call(delivery, "message", subagent_id="parent", message="Progress while still working")
    with sqlite3.connect(home / "state.db") as conn:
        event = json.loads(conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id=?", (receipt["message_id"],)).fetchone()[0])
    assert event["agent_message"] and event["scope_id"] == "tenant-a"
    text = format_process_notification(event)
    assert "AGENT COORDINATION MESSAGE" in text and "ASYNC DELEGATION COMPLETE" not in text
    assert TimelineNotification.for_delegation(text, event).display_kind == "agent_message"
    # Replay has no short outage expiry: the mailbox remains the receipt authority.
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("UPDATE async_delegations SET completed_at=1,dispatched_at=1,updated_at=1 WHERE delegation_id=?", (receipt["message_id"],))
    q = queue.Queue()
    restore_undelivered_completions(q)
    assert any(item["delegation_id"] == receipt["message_id"] for item in list(q.queue))


def test_paused_recipient_keeps_mail_without_resuming(family):
    from tools.delegate_project_store import ProjectStore
    from tools.delegate_projects import recover_projects

    home, parent, (supply, _), _db = family
    with ProjectStore(parent) as store:
        store.pause(supply.session_id)
    receipt = call(parent, "message", subagent_id=supply.session_id, message="Keep this until explicit resume")
    recover_projects(parent)
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegated_project_messages").fetchone()[0] == 0
        assert conn.execute("SELECT delivered_at FROM delegation_mail WHERE message_id=?", (receipt["message_id"],)).fetchone()[0] is None


def test_mail_schema_upgrades_legacy_history_without_platform_ids(tmp_path):
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("main", source="cli")
        db.append_message("main", "user", "Original history")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX messages_platform_id")
        conn.execute("DROP INDEX idx_messages_platform_msg_id")
        conn.execute("ALTER TABLE messages DROP COLUMN platform_message_id")
    with SessionDB(db_path=path) as db:
        assert db.get_messages("main")[0]["content"] == "Original history"
        assert db._conn.execute("SELECT COUNT(*) FROM delegation_mail").fetchone()[0] == 0
        db.append_message("main", "user", "New agent mail", platform_message_id="mail-id")
        assert db._conn.execute("SELECT content FROM messages WHERE platform_message_id='mail-id'").fetchone()[0] == "New agent mail"
