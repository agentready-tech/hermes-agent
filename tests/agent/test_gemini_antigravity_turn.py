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
        def spawn(self, owner, generation, operation, argv):
            mode, response = next(self.steps)
            assert (owner, generation, operation) == (home, "fixture-generation", "inference")
            prompt = json.loads(argv[-1]); rows = prompt["messages"]
            if self.processes:
                with SessionDB(db_path) as reader:
                    persisted = reader.get_messages(session)
                    assert persisted[-1]["role"] == "tool"
                    assert persisted[-1]["tool_call_id"] == rows[-1]["tool_call_id"] == calls[-1]
            child = spawn(tmp_path, mode, response, argv[-1])
            self.processes.append(child)
            return child

    def start(*steps):
        authority = NativeAuthority(home, steps)
        monkeypatch.setattr(agy_runtime, "admit", lambda *_: authority)
        agent = AIAgent(provider=PROVIDER, model=MODEL, quiet_mode=True, session_id=session,
                        session_db=db, skip_context_files=True, skip_memory=True,
                        max_iterations=4, enabled_toolsets=["fixture_echo"])
        agent.valid_tool_names = ["fixture_echo"]
        return agent, authority

    yield start, calls, db, db_path, session
    db.close()


def test_two_echoes_replayed_in_three_children_and_reopened_history(native):
    start, calls, db, path, session = native
    agent, auth = start(proposal("echo-1"), proposal("echo-2"), ("canonical-followup", "echo-2"))
    try:
        result = agent.run_conversation("Echo two inert values")
        assert result["final_response"] == "Проверено ☃", result.get("error")
        assert calls == ["echo-1", "echo-2"]
        assert len({child.pid for child in auth.processes}) == 3
    finally:
        agent.close()
    db.close()
    with SessionDB(path) as reopened:
        rows = reopened.get_messages(session)
        assert [row["tool_calls"][0]["id"] for row in rows if row.get("tool_calls")] == calls
        assert [row["tool_call_id"] for row in rows if row["role"] == "tool"] == calls
        assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == "Проверено ☃"


@pytest.mark.parametrize("text", ["echo-1", "changed"])
def test_duplicate_id_is_not_renamed_or_dispatched(native, text):
    start, calls, db, _, session = native
    agent, auth = start(proposal("echo-1"), proposal("echo-1", text))
    try:
        result = agent.run_conversation("Repeat only through the fixture")
        assert result.get("failure_reason") == "duplicate_tool_call", result
        assert calls == ["echo-1"] and len(auth.processes) == 2
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
