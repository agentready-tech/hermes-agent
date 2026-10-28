"""The opt-in stop reviewer continues work without introducing a persistent goal."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import continuation_guard
from agent.turn_stop_gates import apply_stop_gates
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home


def configure(enabled: bool = True, max_continuations: int = 3) -> None:
    atomic_config_write(get_hermes_home() / "config.yaml", {
        "agent": {"continuation_guard": {
            "enabled": enabled, "max_continuations": max_continuations,
        }},
    })


@pytest.fixture
def agent():
    return SimpleNamespace(
        session_id="guard-session", platform="telegram", side_agent=False,
        _interrupt_requested=False, _persist_disabled=False,
        _emit_interim_assistant_message=Mock(),
        _flush_messages_to_session_db=Mock(),
        _interim_content_was_streamed=Mock(return_value=False),
        _emit_diagnostic_status=Mock(),
    )


@pytest.fixture
def reviewer(monkeypatch):
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
        "verdict": "continue", "next_action": "Check pipeline 42 for commit abc.",
    })))])
    call = Mock(return_value=reply)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", call)
    return call


def transcript():
    return [
        {"role": "user", "content": "Push the fix and verify its CI."},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "push-1", "type": "function", "function": {
                "name": "terminal", "arguments": '{"command":"git push"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "push-1", "content": "Pushed commit abc; pipeline 42 started."},
    ]


def test_disabled_by_default_never_calls_reviewer(agent, reviewer):
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed the fix.") is None
    reviewer.assert_not_called()


def test_enabled_uses_tool_evidence_and_can_be_disabled_without_recreating_agent(agent, reviewer):
    configure()
    nudge = continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed the fix.")
    assert "pipeline 42" in nudge
    payload = json.loads(reviewer.call_args.kwargs["messages"][1]["content"])
    assert payload["request"] == "Push the fix and verify its CI."
    assert any(row.get("tool_call_id") == "push-1" for row in payload["recent_messages"])
    assert "pipeline 42 started" in payload["recent_messages"][-1]["content"]
    assert reviewer.call_args.kwargs["task"] == "goal_judge"

    configure(enabled=False)
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Still checking.") is None
    assert reviewer.call_count == 1


@pytest.mark.parametrize("response", [
    '{"verdict":"stop","reason":"The scheduled check is registered."}',
    '{"verdict":"continue"}',
    "not JSON",
])
def test_wait_or_unusable_decision_does_not_create_work(agent, reviewer, response):
    configure()
    reviewer.return_value.choices[0].message.content = response
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Waiting for CI.") is None


def test_reviewer_failure_does_not_start_a_retry_loop(agent, reviewer):
    configure()
    reviewer.side_effect = TimeoutError("review timed out")
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed.") is None
    assert reviewer.call_count == 1


@pytest.mark.parametrize("attribute", [
    "_interrupt_requested", "side_agent", "_persist_disabled",
    "_run_budget_wrapup_injected", "_tool_guardrail_halt_decision",
])
def test_stopped_or_internal_work_is_not_restarted(agent, reviewer, attribute):
    configure()
    setattr(agent, attribute, True)
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed.") is None
    reviewer.assert_not_called()


def test_delegated_child_does_not_run_parent_guard(agent, reviewer):
    from agent.delegation_context import delegated_child_context

    configure()
    with delegated_child_context():
        assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed.") is None
    reviewer.assert_not_called()


def test_pause_is_respected(agent, reviewer):
    from agent.estop import engage

    configure()
    engage("operator paused work")
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed.") is None
    reviewer.assert_not_called()


@pytest.mark.parametrize("paused", [False, True])
def test_existing_goal_retains_ownership_of_continuation(agent, reviewer, paused):
    from hermes_cli.goals import GoalManager

    configure()
    goal = GoalManager(agent.session_id)
    goal.set("Verify CI")
    if paused:
        goal.pause()
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed.") is None
    reviewer.assert_not_called()


@pytest.mark.parametrize("change", ["stop", "disable", "pause"])
def test_inflight_review_cannot_override_new_stop_or_disable(agent, reviewer, change):
    from agent.estop import engage

    configure()
    reply = reviewer.return_value

    def review(**kwargs):
        if change == "stop":
            agent._interrupt_requested = True
        elif change == "disable":
            configure(enabled=False)
        else:
            engage("paused while review was running")
        return reply

    reviewer.side_effect = review
    assert continuation_guard.build_continuation_nudge(agent, transcript(), "Pushed.") is None


def test_real_stop_gate_continues_in_order_and_honors_limit(agent, reviewer, monkeypatch):
    from agent.session_persistence import _is_ephemeral_scaffolding
    from agent.turn_finalizer import _drop_verification_continuation_scaffolding

    configure(max_continuations=2)
    monkeypatch.setattr("agent.delegation_mail.deliver", lambda *args, **kwargs: False)
    messages = transcript()
    original = list(messages)
    candidate = "Pushed the fix; CI remains unchecked."

    for expected in (True, True, False):
        verdict = apply_stop_gates(
            agent, {"role": "assistant", "content": candidate},
            final_response=candidate, messages=messages, conversation_history=[],
            pending_verification_response=None,
            pending_verification_response_previewed=False,
        )
        assert verdict.continue_turn is expected
        if expected:
            assert verdict.final_response is None
            assert [row["role"] for row in messages[-2:]] == ["assistant", "user"]
            assert _is_ephemeral_scaffolding(messages[-1])
            assert not _is_ephemeral_scaffolding(messages[-2])

    assert reviewer.call_count == 2
    assert messages[:len(original)] == original
    _drop_verification_continuation_scaffolding(messages)
    assert all(not _is_ephemeral_scaffolding(row) for row in messages)
    assert candidate in [row.get("content") for row in messages]
