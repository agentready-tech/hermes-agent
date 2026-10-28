"""Exercise optional stop review through the real conversation loop with controlled models."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home
from run_agent import AIAgent


def configure(enabled=True):
    atomic_config_write(get_hermes_home() / "config.yaml", {
        "agent": {"continuation_guard": {"enabled": enabled, "max_continuations": 1}},
    })


def response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None), finish_reason="stop")],
        model="test/model", usage=None,
    )


@pytest.fixture
def agent():
    configure()
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(
            session_id="continuation-loop", api_key="test-key",
            base_url="https://example.invalid/v1", provider="openai-compat",
            model="test/model", max_iterations=6, quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
    instance._cached_system_prompt = "Stable instructions."
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_args, **_kwargs: None
    instance._save_trajectory = lambda *_args, **_kwargs: None
    return instance


def test_real_loop_continues_then_resets_budget_for_next_user_turn(agent, monkeypatch):
    replies = iter([
        response("The commit is pushed. Its CI still needs verification."),
        response("Pipeline 42 passed for commit abc."),
        response("The second commit is pushed. Its CI still needs verification."),
        response("Pipeline 43 passed for commit def."),
    ])
    agent._interruptible_api_call = Mock(side_effect=lambda _kwargs: next(replies))
    reviewer = Mock(return_value=response('{"verdict":"continue","next_action":"Verify the pipeline."}'))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", reviewer)

    first = agent.run_conversation("Push the first fix and verify CI.")
    second = agent.run_conversation("Push the second fix and verify CI.", conversation_history=first["messages"])

    assert first["final_response"] == "Pipeline 42 passed for commit abc."
    assert second["final_response"] == "Pipeline 43 passed for commit def."
    assert agent._interruptible_api_call.call_count == 4
    assert reviewer.call_count == 2  # one correction per user turn, not one per session
    assert agent._cached_system_prompt == "Stable instructions."
    assert all(not row.get("_verification_stop_synthetic") for row in second["messages"])


def test_switching_off_restores_single_response_without_auxiliary_call(agent, monkeypatch):
    reviewer = Mock(return_value=response('{"verdict":"stop"}'))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", reviewer)
    agent._interruptible_api_call = Mock(return_value=response("The requested analysis is complete."))

    first = agent.run_conversation("Analyze this proposal.")
    assert reviewer.call_count == 1
    configure(enabled=False)
    second = agent.run_conversation("Explain the conclusion.", conversation_history=first["messages"])

    assert second["final_response"] == "The requested analysis is complete."
    assert reviewer.call_count == 1
    assert agent._interruptible_api_call.call_count == 2


def test_exhausted_iteration_budget_does_not_pay_for_an_unusable_review(agent, monkeypatch):
    agent.max_iterations = 1
    agent.iteration_budget.max_total = 1
    agent._interruptible_api_call = Mock(return_value=response("The commit is pushed; CI is unchecked."))
    reviewer = Mock()
    monkeypatch.setattr("agent.auxiliary_client.call_llm", reviewer)

    result = agent.run_conversation("Push the fix and verify CI.")

    assert result["final_response"] == "The commit is pushed; CI is unchecked."
    assert agent._interruptible_api_call.call_count == 1
    reviewer.assert_not_called()
