"""Real return paths, failure recovery and stop limits for native tool receipts."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def operation_home(monkeypatch):
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session

    with tempfile.TemporaryDirectory(prefix="hreturn-") as directory:
        home = Path(directory)
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(hermes, "_home", home.resolve())
        monkeypatch.setattr("hermes_runtime.adapters.turns.forward_to_active_turn", lambda *_: True)
        ensure_home_session()
        yield home


def owner(receipt):
    return SimpleNamespace(session_id="agentready_home", _agentready_active_turn=object(),
        _managed_tool_receipts=[{"tool": "browser_exec", "tool_call_id": "skipped", **receipt}])


def admit(store, uid="uid", *, session="agentready_home", group="group"):
    row, execute = store.admit(session, uid, uid, "browser_exec", {}, "desktop", group)
    assert execute
    return row["operation_id"]


def test_busy_resource_is_not_a_subscription_and_cannot_witness_a_return(operation_home):
    from agent.tool_continuation import continuation_evidence, review_tool_continuation
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    store = ToolOperationStore(operation_home)
    identity = admit(store)
    blocked, execute = store.admit("agentready_home", "next", "next", "browser_exec", {}, "desktop", "other")
    assert not execute and blocked["waiting_for"] == identity
    assert blocked["reason"] == "resource_busy"
    agent = owner(blocked)
    assert not continuation_evidence(agent)[0]["return_guaranteed"]
    nudge, notice = review_tool_continuation(agent)
    assert nudge and not notice
    assert store.get(identity)["state"] == "running"
    assert store.get(identity)["deferred"] == 0  # checking never fabricates a subscription


def test_only_a_live_deferred_owner_in_the_same_session_witnesses_a_return(operation_home):
    from agent.tool_continuation import continuation_evidence, review_tool_continuation
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    store = ToolOperationStore(operation_home)
    identity = admit(store)
    store.defer(identity)
    agent = owner({"status": "not_started", "waiting_for": identity})
    state = continuation_evidence(agent)[0]
    assert state["return_path"] == "deferred_operation"
    assert review_tool_continuation(agent) == (None, None)
    agent.session_id = "another-conversation"
    assert not continuation_evidence(agent)[0]["return_guaranteed"]
    assert review_tool_continuation(agent)[0]


@pytest.mark.parametrize("failed", [False, True])
def test_success_and_failure_have_one_durable_completion_until_recorded(operation_home, failed):
    from agent.tool_continuation import continuation_evidence
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    from hermes_state import SessionDB

    store = ToolOperationStore(operation_home)
    identity = admit(store)
    store.defer(identity)
    store.complete(identity, {"error": "failed"} if failed else {"result": "done"}, failed=failed)
    store.recover()
    store.recover()
    inbox = HomeInbox(operation_home)
    pending = inbox.pending()
    assert len(pending) == 1
    agent = owner({"status": "running", "operation_id": identity})
    state = continuation_evidence(agent)[0]
    assert state["state"] == ("failed" if failed else "completed")
    assert state["return_path"] == "durable_inbox"
    receipt_id, content = pending[0]
    with SessionDB() as db:
        db.append_messages_batch(agent.session_id, [
            {"role": "user", "content": content, "platform_message_id": receipt_id}])
    inbox.acknowledge_recorded()
    store.recover()
    assert inbox.pending() == []
    assert not continuation_evidence(agent)[0]["return_guaranteed"]
    assert store.get(identity)["delivered_at"] is not None


def test_terminal_result_without_an_enqueued_event_is_not_a_guaranteed_return(operation_home, monkeypatch):
    from agent.tool_continuation import continuation_evidence
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    store = ToolOperationStore(operation_home)
    identity = admit(store)
    store.defer(identity)
    publish = store.publish
    monkeypatch.setattr(store, "publish", Mock(side_effect=OSError("delivery unavailable")))
    with pytest.raises(OSError):
        store.complete(identity, "saved result")
    agent = owner({"status": "running", "operation_id": identity})
    assert not continuation_evidence(agent)[0]["return_guaranteed"]
    monkeypatch.setattr(store, "publish", publish)
    ToolOperationStore(operation_home).recover()
    assert continuation_evidence(agent)[0]["return_path"] == "durable_inbox"
    assert len(HomeInbox(operation_home).pending()) == 1


def test_timeout_and_restart_preserve_unknown_outcome_without_reexecuting(operation_home, monkeypatch):
    from agent.tool_continuation import continuation_evidence
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    store = ToolOperationStore(operation_home)
    identity = admit(store)
    store.defer(identity)
    agent = owner({"status": "outcome_unknown", "operation_id": identity})
    assert continuation_evidence(agent)[0]["return_path"] == "deferred_operation"
    monkeypatch.setattr("hermes_runtime.adapters.tool_operations._owner_alive", lambda *_: False)
    assert not continuation_evidence(agent)[0]["return_guaranteed"]
    restarted = ToolOperationStore(operation_home)
    restarted.recover()
    restarted.recover()
    assert continuation_evidence(agent)[0]["state"] == "outcome_unknown"
    assert continuation_evidence(agent)[0]["return_path"] == "durable_inbox"
    replay, execute = restarted.admit("agentready_home", "uid", "uid", "browser_exec", {}, "desktop", "new")
    assert not execute and replay["operation_id"] == identity
    assert len(HomeInbox(operation_home).pending()) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_late_timeout_result_is_delivered_without_retrying_the_operation(operation_home, failed):
    from agent.tool_continuation import continuation_evidence
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    store = ToolOperationStore(operation_home)
    identity = admit(store)
    store.defer(identity)
    agent = owner({"status": "outcome_unknown", "operation_id": identity})
    store.complete(identity, "late failure" if failed else "late result", failed=failed)
    assert continuation_evidence(agent)[0]["return_path"] == "durable_inbox"
    _, execute = store.admit("agentready_home", "uid", "uid", "browser_exec", {}, "desktop", "retry")
    assert not execute
    assert len(HomeInbox(operation_home).pending()) == 1


@pytest.mark.parametrize("attribute,value", [
    ("_interrupt_requested", True), ("_persist_disabled", True), ("side_agent", True),
    ("platform", "cron"),
])
def test_native_recheck_respects_stop_and_internal_turns(operation_home, attribute, value):
    from agent.tool_continuation import review_tool_continuation

    agent = owner({"status": "not_started", "reason": "incoming_event"})
    setattr(agent, attribute, value)
    assert review_tool_continuation(agent) == (None, None)


def test_paused_goal_does_not_restart_skipped_work(operation_home):
    from agent.tool_continuation import review_tool_continuation
    from hermes_cli.goals import GoalManager

    agent = owner({"status": "not_started", "reason": "incoming_event"})
    goal = GoalManager(agent.session_id)
    goal.set("Wait for the user")
    goal.pause()
    assert review_tool_continuation(agent) == (None, None)


def test_unconfirmed_database_state_cannot_witness_a_wakeup(operation_home, monkeypatch):
    import sqlite3
    from agent.tool_continuation import continuation_evidence, review_tool_continuation

    monkeypatch.setattr("hermes_runtime.adapters.tool_operations.ToolOperationStore.continuation_state",
        Mock(side_effect=sqlite3.OperationalError("database unavailable")))
    agent = owner({"status": "running", "operation_id": "unconfirmed"})
    assert not continuation_evidence(agent)[0]["return_guaranteed"]
    assert review_tool_continuation(agent)[0]


def test_zero_continuation_limit_reports_incomplete_work_without_restarting(operation_home):
    from agent.tool_continuation import review_tool_continuation
    from hermes_cli.config import atomic_config_write

    atomic_config_write(operation_home / "config.yaml", {
        "agent": {"continuation_guard": {"max_continuations": 0}}})
    nudge, notice = review_tool_continuation(owner({"status": "not_started"}))
    assert nudge is None
    assert "incomplete" in notice


def test_terminal_notice_preserves_the_promoted_api_carrier(operation_home, monkeypatch):
    from agent.turn_stop_gates import apply_stop_gates

    agent = owner({"status": "not_started"})
    agent._managed_tool_recheck_requested = True
    monkeypatch.setattr("agent.delegation_mail.deliver", lambda *_a, **_kw: False)
    for name in ["_verify_on_stop_nudge", "_pre_verify_nudge", "_kanban_stop_nudge"]:
        monkeypatch.setattr("agent.turn_stop_gates." + name, lambda *_a, **_kw: None)
    candidate = {"role": "assistant", "content": "", "api_content": "I will report later."}
    verdict = apply_stop_gates(agent, candidate, final_response=candidate["api_content"],
        messages=[], conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False)
    assert not verdict.continue_turn
    assert "incomplete" in verdict.final_response
    assert candidate["content"] == ""
    assert candidate["api_content"] == verdict.final_response


def test_decision_payload_contains_verified_state_not_just_receipt_prose(operation_home, monkeypatch):
    from agent.continuation_guard import build_continuation_nudge
    from hermes_cli.config import atomic_config_write
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    atomic_config_write(operation_home / "config.yaml", {"agent": {"continuation_guard": {"enabled": True}}})
    store = ToolOperationStore(operation_home)
    identity = admit(store)
    agent = owner({"status": "running", "operation_id": identity, "message": "A return is guaranteed."})
    judge = Mock(return_value=None)
    monkeypatch.setattr("agent.continuation_guard._next_action", judge)
    assert build_continuation_nudge(agent, [], "Waiting.", user_message="Do the work") is None
    evidence = judge.call_args.args[1]["tool_continuations"][0]
    assert evidence["state"] == "running"
    assert not evidence["return_guaranteed"]
    assert "message" not in evidence
    assert "arguments_json" not in json.dumps(evidence)
