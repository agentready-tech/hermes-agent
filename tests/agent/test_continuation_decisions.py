"""Decisions uses the owning profile's key and never turns a broken reply into work."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agent import continuation_guard
from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home


@pytest.fixture
def runtime(monkeypatch):
    atomic_config_write(get_hermes_home() / "config.yaml", {
        "agent": {"continuation_guard": {"enabled": True, "judge": "openrouter_decisions"}},
    })
    monkeypatch.setenv("OPENROUTER_API_KEY", "another-profile-key")
    legacy = Mock(side_effect=AssertionError("Decisions must not fall back to the main model"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", legacy)
    agent = SimpleNamespace(session_id="decisions-test", platform="local", _interrupt_requested=False)
    return agent, legacy


def install_response(monkeypatch, callback):
    original = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        return callback(request)

    class Client(original):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    return calls


def review(agent):
    request = "Push the fix and verify CI passes."
    messages = [{"role": "user", "content": request},
                {"role": "tool", "tool_call_id": "push", "content": "Pushed; CI run 42 created, result not checked."}]
    token = set_secret_scope({"OPENROUTER_API_KEY": "owning-profile-key"})
    try:
        return continuation_guard.build_continuation_nudge(agent, messages, "Pushed; CI remains unchecked.")
    finally:
        reset_secret_scope(token)


@pytest.mark.parametrize("choice", ["stop", "continue"])
def test_profile_config_routes_binary_decision_without_a_text_next_action(runtime, monkeypatch, choice):
    agent, legacy = runtime
    calls = install_response(monkeypatch, lambda request: httpx.Response(200, json={
        "model": "openai/gpt-6-luna-decisions-snapshot", "provider": "OpenAI", "id": "decision-1",
        "answers": {"verdict": {"type": "choice", "choice": choice}},
        "usage": {"input_tokens": 500, "output_tokens": 0, "cost": 0.00005},
    }))
    nudge = review(agent)
    assert bool(nudge) is (choice == "continue")
    if nudge:
        assert "wakeup" in nudge and "user" in nudge
    assert len(calls) == 1
    assert str(calls[0].url) == "https://openrouter.ai/api/alpha/decisions"
    assert calls[0].headers["Authorization"] == "Bearer owning-profile-key"
    assert 0 < calls[0].extensions["timeout"]["read"] <= 10
    body = json.loads(calls[0].content)
    assert body["model"] == "openai/gpt-6-luna-decisions"
    assert set(body["questions"]["verdict"]["criteria"]) == {"stop", "continue"}
    assert body["state"]["recent_messages"][-1]["tool_call_id"] == "push"
    legacy.assert_not_called()


@pytest.mark.parametrize("failure", ["http", "timeout", "json", "missing", "wrong_type", "unknown"])
def test_unusable_decision_allows_stop_without_retry_or_fallback(runtime, monkeypatch, failure):
    agent, legacy = runtime

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "http":
            return httpx.Response(429, json={"error": "rate limited"})
        if failure == "json":
            return httpx.Response(200, content=b"not JSON")
        if failure == "missing":
            return httpx.Response(200, json={"answers": {}})
        return httpx.Response(200, json={"answers": {"verdict": {
            "type": "choice" if failure == "unknown" else "refusal",
            "choice": "wait" if failure == "unknown" else "continue",
        }}})

    calls = install_response(monkeypatch, respond)
    assert review(agent) is None
    assert len(calls) == 1
    legacy.assert_not_called()


def test_missing_profile_key_does_not_send_a_request(runtime, monkeypatch):
    agent, legacy = runtime
    monkeypatch.setattr("agent.secret_scope.serves_routed_profile", lambda: True)
    calls = install_response(monkeypatch, lambda request: pytest.fail("Missing key must not reach HTTP"))
    assert review_without_key(agent) is None
    assert calls == []
    legacy.assert_not_called()


def review_without_key(agent):
    token = set_secret_scope({})
    try:
        return continuation_guard.build_continuation_nudge(agent, [{"role": "user", "content": "Verify CI"}], "Unchecked.")
    finally:
        reset_secret_scope(token)


@pytest.mark.parametrize("change", ["disable", "pause", "judge"])
def test_inflight_decision_respects_operator_changes(runtime, monkeypatch, change):
    agent, _legacy = runtime

    def respond(request):
        if change == "pause":
            agent._interrupt_requested = True
        else:
            settings = {"enabled": change != "disable", "judge": "goal_judge" if change == "judge" else "openrouter_decisions"}
            atomic_config_write(get_hermes_home() / "config.yaml", {"agent": {"continuation_guard": settings}})
        return httpx.Response(200, json={"answers": {"verdict": {"type": "choice", "choice": "continue"}}})

    calls = install_response(monkeypatch, respond)
    assert review(agent) is None
    assert len(calls) == 1


def test_decisions_preserves_auxiliary_observation_without_main_request_events(runtime, monkeypatch):
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    manager = PluginManager()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(plugins_mod, "_plugin_managers_by_home", {})
    context = PluginContext(PluginManifest(name="decision-observer", source="user"), manager)
    fired = []
    for event in ("pre_auxiliary_call", "post_auxiliary_call", "pre_api_request", "post_api_request"):
        context.register_hook(event, lambda _event=event, **kw: fired.append((_event, kw)))
    install_response(monkeypatch, lambda request: httpx.Response(200, json={
        "answers": {"verdict": {"type": "choice", "choice": "stop"}},
        "usage": {"input_tokens": 300, "output_tokens": 0, "cost": 0.00003},
    }))
    assert review(runtime[0]) is None
    assert [event for event, _ in fired] == ["pre_auxiliary_call", "post_auxiliary_call"]
    assert all(data["aux_task"] == "goal_judge" and data["api_mode"] == "decisions" for _, data in fired)
    assert fired[1][1]["usage"]["prompt_tokens"] == 300
