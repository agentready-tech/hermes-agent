"""Skipped managed calls cannot end a turn on an imaginary completion event."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletion


@pytest.fixture
def managed_agent(monkeypatch):
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_state import SessionDB
    from run_agent import AIAgent

    with tempfile.TemporaryDirectory(prefix="hstop-") as directory:
        home = Path(directory)
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(hermes, "_home", home.resolve())
        ensure_home_session()
        db = SessionDB()
        with patch("agent.process_bootstrap.OpenAI"):
            agent = AIAgent(api_key="test-only", base_url="http://127.0.0.1:9/v1", provider="custom",
                model="test/model", quiet_mode=True, session_db=db, session_id="agentready_home",
                skip_context_files=True, skip_memory=True, enabled_toolsets=["web"])
        agent.compression_enabled, agent.save_trajectories, agent.tool_delay = False, False, 0
        yield agent, db, home
        db.close()
        from hermes_logging import _reset_queued_handlers
        _reset_queued_handlers()


def reply(text=None, *, call_id=None):
    message = {"role": "assistant", "content": text}
    if call_id:
        message["tool_calls"] = [{"id": call_id, "type": "function", "function": {
            "name": "web_search", "arguments": '{"query":"requested work"}'}}]
    return ChatCompletion(id="test-reply", object="chat.completion", created=1, model="test/model",
        choices=[{"index": 0, "message": message, "finish_reason": "tool_calls" if call_id else "stop"}],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


@pytest.mark.parametrize("judge", ["stop", "error", "disabled"])
def test_incoming_before_dispatch_rechecks_unstarted_work_without_a_wakeup(managed_agent, monkeypatch, judge):
    from hermes_cli.config import atomic_config_write
    from hermes_runtime.adapters.turns import forward_to_active_turn

    agent, db, home = managed_agent
    atomic_config_write(home / "config.yaml", {"agent": {"continuation_guard": {
        "enabled": judge != "disabled", "judge": "openrouter_decisions", "max_continuations": 3,
    }}})
    reviewer = Mock(return_value="stop", side_effect=TimeoutError("judge unavailable") if judge == "error" else None)
    monkeypatch.setattr("agent.continuation_decisions.judge_continuation", reviewer)
    requests, executed = [], []

    def model(payload, *args, **kwargs):
        requests.append(payload["messages"])
        if len(requests) == 1:
            assert forward_to_active_turn("Give a status update and continue the requested work.")
            return reply(call_id="skipped")
        if len(requests) == 2:
            return reply("I am doing it; I will report later.")
        if len(requests) == 3:
            return reply(call_id="retry")
        return reply("The requested work is complete.")

    def handler(name, args, *a, **kw):
        executed.append(name)
        return '{"result":"requested work completed"}'

    monkeypatch.setattr(agent, "_interruptible_api_call", model)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", model)
    monkeypatch.setattr("model_tools.handle_function_call", handler)

    result = agent.run_conversation("Do the requested work.")

    assert result["final_response"] == "The requested work is complete."
    assert executed == ["web_search"]  # the skipped call never executed; retry executes once
    rows = db.get_messages_as_conversation(agent.session_id)
    skipped = next(r for r in rows if r.get("tool_call_id") == "skipped" and r["role"] == "tool")
    assert "completion event" not in skipped["content"].lower()
    assert '"reason": "incoming_event"' in skipped["content"]
    assert any(r.get("finish_reason") == "continuation_required" for r in rows)
    assert all(not r.get("_verification_stop_synthetic") for r in result["messages"])


@pytest.mark.parametrize("budget,expected_calls", [(None, 3), (2, 2)])
def test_model_ignoring_the_recheck_gets_a_bounded_visible_incomplete_outcome(managed_agent, monkeypatch, budget, expected_calls):
    from hermes_runtime.adapters.turns import forward_to_active_turn

    agent, db, _ = managed_agent
    if budget is not None:
        agent.max_iterations = agent.iteration_budget.max_total = budget
    requests = []

    def model(payload, *args, **kwargs):
        requests.append(payload)
        if len(requests) == 1:
            assert forward_to_active_turn("Continue and report status.")
            return reply(call_id="skipped")
        return reply("I will report later.")

    monkeypatch.setattr(agent, "_interruptible_api_call", model)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", model)
    handler = Mock(side_effect=AssertionError("A skipped call must not execute"))
    monkeypatch.setattr("model_tools.handle_function_call", handler)

    result = agent.run_conversation("Do the requested work.")

    assert len(requests) == expected_calls
    assert "no confirmed automatic continuation" in result["final_response"].lower()
    assert "incomplete" in result["final_response"].lower()
    assert db.get_messages_as_conversation(agent.session_id)[-1]["content"] == result["final_response"]
    handler.assert_not_called()
