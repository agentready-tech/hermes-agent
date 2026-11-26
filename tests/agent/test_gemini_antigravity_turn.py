"""Native Hermes dispatch, append, replay and reopen for the inert AGE-11 provider."""

import json

import pytest

from hermes_runtime.standalone import agy_runtime
from hermes_state import SessionDB
from providers.agy_policy import MODEL, PROVIDER
from tests.agent.agy_fixture import spawn
from tests.agent.test_gemini_antigravity_transport import Authority, SCHEMA, TOOLS

pytestmark = pytest.mark.platforms("posix")


def proposal(identity, text=None, *, name="fixture_echo"):
    return "ok", json.dumps({"type": "tool_call", "id": identity, "name": name,
                              "arguments": {"text": identity if text is None else text}})


@pytest.fixture
def native(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent
    from tools.registry import registry
    import model_tools

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    session = "inert-native-session"
    calls = []
    home = get_hermes_home()
    def echo(args, **_kwargs):
        with SessionDB(db_path) as reader:
            rows = reader.get_messages(session)
            assert rows[-1]["role"] == "assistant"
            assert rows[-1]["tool_calls"][0]["id"] == args["text"]
        calls.append(args["text"])
        return json.dumps(args)

    registry.register("fixture_echo", "fixture_echo", {"name": "fixture_echo", "parameters": SCHEMA}, echo)
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda *_, **__: TOOLS)

    class NativeAuthority(Authority):
        def spawn(self, owner, generation, operation, payload):
            mode, response = next(self.steps)
            assert (owner, generation, operation) == (home, "fixture-generation", "inference")
            prompt = json.loads(payload); rows = prompt["messages"]
            if self.processes:
                with SessionDB(db_path) as reader:
                    persisted = reader.get_messages(session)
                    assert persisted[-1]["role"] == "tool"
                    assert persisted[-1]["tool_call_id"] == rows[-1]["tool_call_id"] == calls[-1]
            child = spawn(tmp_path, mode, response, payload if mode == "canonical-followup" else "")
            self.processes.append(child)
            return child

    def start(*steps, active_db=db):
        authority = NativeAuthority(home, steps)
        monkeypatch.setattr(agy_runtime, "admit", lambda *_: authority)
        agent = AIAgent(provider=PROVIDER, model=MODEL, quiet_mode=True, session_id=session,
                        session_db=active_db, skip_context_files=True, skip_memory=True,
                        max_iterations=4, enabled_toolsets=["fixture_echo"])
        agent.valid_tool_names = ["fixture_echo"]
        return agent, authority

    yield start, calls, db, db_path, session
    db.close()


@pytest.mark.parametrize(("source_prefix", "stored_prefix"), [("echo-", "echo-"), ("call_", "agy-")])
def test_two_echoes_replayed_in_three_children_and_reopened_history(native, source_prefix, stored_prefix):
    start, calls, db, path, session = native
    first, second = f"{stored_prefix}1", f"{stored_prefix}2"
    agent, auth = start(proposal(f"{source_prefix}1", first), proposal(f"{source_prefix}2", second),
                        ("canonical-followup", second))
    try:
        result = agent.run_conversation("Echo two inert values")
        assert result["final_response"] == "Проверено ☃", result.get("error")
        assert calls == [first, second]
        assert len({child.pid for child in auth.processes}) == 3
    finally:
        agent.close()
    db.close()
    with SessionDB(path) as reopened:
        rows = reopened.get_messages(session)
        assert [row["tool_calls"][0]["id"] for row in rows if row.get("tool_calls")] == calls
        assert [row["tool_call_id"] for row in rows if row["role"] == "tool"] == calls
        assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == "Проверено ☃"


@pytest.mark.parametrize(("source", "duplicate", "stored", "text"), [
    ("echo-1", "echo-1", "echo-1", "echo-1"),
    ("echo-1", "echo-1", "echo-1", "changed"),
    ("call_1", "call_1", "agy-1", "agy-1"),
    ("call_1", "agy-1", "agy-1", "changed"),
])
def test_duplicate_id_is_not_renamed_or_dispatched(native, source, duplicate, stored, text):
    start, calls, db, _, session = native
    agent, auth = start(proposal(source, stored), proposal(duplicate, text))
    try:
        result = agent.run_conversation("Repeat only through the fixture")
        assert result.get("failure_reason") == "duplicate_tool_call", result
        assert calls == [stored] and len(auth.processes) == 2
        assert sum(bool(row.get("tool_calls")) for row in db.get_messages(session)) == 1
    finally:
        agent.close()


@pytest.mark.parametrize("step", [proposal("echo-1", name="other"), proposal("echo-1", 17)])
def test_rejected_proposal_never_reaches_the_registry(native, step):
    start, calls, _, _, _ = native
    agent, auth = start(step)
    try:
        result = agent.run_conversation("Benign fixture")
        assert result.get("failure_reason") == "protocol", result
        assert not calls and len(auth.processes) == 1
    finally:
        agent.close()


@pytest.mark.parametrize("flush", [None, False])
def test_unproven_append_blocks_dispatch(native, monkeypatch, flush):
    start, calls, _, _, _ = native
    agent, auth = start(proposal("echo-1"))
    original = agent._flush_messages_to_session_db
    def fail(rows, *args):
        if rows[-1].get("tool_calls"):
            return flush
        return original(rows, *args)
    monkeypatch.setattr(agent, "_flush_messages_to_session_db", fail)
    try:
        result = agent.run_conversation("Benign fixture")
        assert result.get("failed") and not calls and len(auth.processes) == 1
    finally:
        agent.close()


@pytest.mark.parametrize("flush", [None, False])
def test_unproven_tool_result_blocks_followup(native, monkeypatch, flush):
    start, calls, _, _, _ = native
    agent, auth = start(proposal("echo-1"))
    original = agent._flush_messages_to_session_db
    def fail(rows, *args):
        return flush if rows[-1]["role"] == "tool" else original(rows, *args)
    monkeypatch.setattr(agent, "_flush_messages_to_session_db", fail)
    try:
        result = agent.run_conversation("Benign fixture")
        assert result.get("failed") and calls == ["echo-1"] and len(auth.processes) == 1
    finally:
        agent.close()


def test_native_stream_failure_is_terminal_and_redacted(native, monkeypatch, caplog):
    start, calls, _, _, _ = native
    agent, auth = start(("nonzero", '{"type":"final","answer":"never publish fixture marker"}'))
    def forbidden(*_args, **_kwargs):
        pytest.fail("switched provider on an ambiguous print outcome")
    monkeypatch.setattr(agent, "_try_activate_fallback", forbidden)
    try:
        result = agent.run_conversation("Benign failure fixture")
        assert result.get("failure_reason") == "transport" and not result.get("failure_retryable")
        assert not calls and len(auth.processes) == 1
        assert "never publish fixture marker" not in result["final_response"] and "never publish fixture marker" not in caplog.text
    finally:
        agent.close()


def test_duplicate_in_compression_parent_is_not_replayed_after_adoption(native):
    from types import SimpleNamespace
    from agent.conversation_compression import _publish_rotated_compaction

    start, calls, db, path, parent = native
    identity = "archived-echo"
    agent, _ = start(proposal(identity), ("canonical-followup", identity))
    try:
        assert agent.run_conversation("Run the fixed echo")["final_response"] == "Проверено ☃"
        stale_rows = db.get_messages_as_conversation(parent)
        handoff = [{"role": "user", "content": "[CONTEXT COMPACTION] The fixed echo finished."},
                   {"role": "assistant", "content": "Ready for the next inert value."}]
        _publish_rotated_compaction(agent, stale_rows, handoff, new_system_prompt="Fixture prompt",
            lease=SimpleNamespace(holder=None, ttl=60.0, watermark=None), old_session_id=parent,
            compressed_user_turn_outcome="none")
        child = agent.session_id
        assert child != parent and db.get_compression_tip(parent) == child
        with SessionDB(path) as reopened:
            assert not any(row.get("tool_calls") for row in reopened.get_messages(child))
            assert [row["tool_call_id"] for row in reopened.get_messages(parent) if row["role"] == "tool"] == [identity]
            contender, authority = start(proposal(identity), active_db=reopened)
            try:
                result = contender.run_conversation("Synthetic retry after restore", conversation_history=stale_rows)
                assert contender.session_id == child
                assert result["failure_reason"] == "duplicate_tool_call"
                assert calls == [identity] and len(authority.processes) == 1
                assert not any(row.get("tool_calls") for row in reopened.get_messages(child))
            finally:
                contender.close()
    finally:
        agent.close()


def test_restart_does_not_dispatch_an_orphaned_assistant_call(native):
    start, calls, db, path, session = native
    identity = "orphan-echo"
    initial, authority = start(proposal(identity))
    original = initial._flush_messages_to_session_db
    def save_then_cancel(rows, *args, **kwargs):
        saved = original(rows, *args, **kwargs)
        if saved is True and rows[-1].get("tool_calls"):
            initial.interrupt()
        return saved
    initial._flush_messages_to_session_db = save_then_cancel
    try:
        initial.run_conversation("Save a benign call and interrupt before dispatch")
        assert not calls and len(authority.processes) == 1
    finally:
        initial.close()
    db.close()

    with SessionDB(path) as reopened:
        persisted = reopened.get_messages(session)
        assert [r["tool_calls"][0]["id"] for r in persisted if r.get("tool_calls")] == [identity]
        resumed, authority = start(proposal(identity), active_db=reopened)
        try:
            result = resumed.run_conversation("Synthetic next turn", conversation_history=reopened.get_messages_as_conversation(session))
            assert result["failure_reason"] == "duplicate_tool_call"
            assert not calls and len(authority.processes) == 1
            assert sum(bool(row.get("tool_calls")) for row in reopened.get_messages(session)) == 1
        finally:
            resumed.close()


@pytest.mark.parametrize("withdraw", [True, False])
def test_pending_echo_loses_authority_inside_native_dispatch(native, monkeypatch, withdraw):
    from agent import tool_executor

    start, calls, _, path, session = native
    agent, authority = start(proposal("pending-echo"))
    original = tool_executor._pre_tool_block
    entered = []

    def withdraw_after_admission(owner, ref):
        assert owner is agent and ref.call_id == "pending-echo"
        with SessionDB(path) as reader:
            assert reader.get_messages(session)[-1]["tool_calls"][0]["id"] == "pending-echo"
        entered.append(ref.call_id)
        if withdraw:
            monkeypatch.setattr(authority, "active", lambda _home: None)
        else:
            agent.interrupt()
        return original(owner, ref)

    monkeypatch.setattr(tool_executor, "_pre_tool_block", withdraw_after_admission)
    try:
        result = agent.run_conversation("Echo only if the fixture generation is still active")
        assert result["failure_reason"] == ("auth_required" if withdraw else "cancelled")
        assert entered == ["pending-echo"] and not calls and len(authority.processes) == 1
    finally:
        agent.close()
    with SessionDB(path) as reopened:
        persisted = reopened.get_messages(session)
        assert [r["tool_calls"][0]["id"] for r in persisted if r.get("tool_calls")] == ["pending-echo"]
        assert not [r for r in persisted if r["role"] == "tool"]
        restarted, authority = start(proposal("pending-echo"), active_db=reopened)
        try:
            assert restarted.run_conversation("Inert echo after reopen")["failure_reason"] == "duplicate_tool_call"
            assert not calls and len(authority.processes) == 1
        finally:
            restarted.close()


def test_valid_final_with_literal_legacy_marker_has_no_retry(native):
    start, _, db, _, session = native
    answer = "Literal marker: <REASONING_SCRATCHPAD>"
    agent, authority = start(("ok", json.dumps({"type": "final", "answer": answer})))
    try:
        result = agent.run_conversation("Show a literal text marker")
        assert result["final_response"] == answer
        assert db.get_messages(session)[-1]["content"] == answer
        assert len(authority.processes) == 1
    finally:
        agent.close()
