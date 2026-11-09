"""Customer subscription isolation, native login reuse and model-route preservation."""

import asyncio
import copy
import json

import pytest

from hermes_runtime.standalone.panel_subscriptions import Subscriptions


@pytest.fixture
def service(tmp_path, monkeypatch):
    home = tmp_path / "customer"
    home.mkdir()
    route = {"provider": "custom", "default": "managed-model", "base_url": "https://panel.invalid/app/api/agents/env_fixture/inference/v1", "key_env": "AGENTSEE_INFERENCE_KEY", "api_mode": "chat_completions"}
    config = {"model": route, "terminal": {"backend": "kubernetes", "kubernetes_pod": "sandbox-0"},
        "autonomous": {"panel": {"url": "https://panel.invalid", "environment_id": "env_fixture"}},
        "stt": {"enabled": True}, "agent": {"continuation_guard": {"enabled": True}}}
    calls = []

    class Auth:
        gateway_owned = False

        def rpc_connection(self):
            return self.rpc_url(), {}

        def authenticated_request(self, path, payload=None, method=None):
            self.rpc_url()
            return self.request(path, payload, method)

        def rpc_url(self):
            return "ws://127.0.0.1:9/api/ws"

        def request(self, path, payload=None, method=None):
            calls.append((path, payload, method))
            if path == "/api/providers/oauth":
                return {"providers": [{"id": "openai-codex", "status": {"logged_in": True, "token_preview": "private-preview", "account_email": "private-email"}}]}
            if path.endswith("/start"):
                return {"session_id": "own-session", "verification_url": "https://auth.openai.com/codex/device", "user_code": "fixture-code", "access_token": "must-not-leave-hermes"}
            if "/poll/" in path:
                return {"status": "approved", "error_message": "private", "account_email": "private"}
            return {"ok": True}

    value = Subscriptions(home, Auth())
    (home / "config.yaml").write_text(json.dumps(config))
    write = value._write
    def persist(data):
        write(data)
        config.clear()
        config.update(copy.deepcopy(data))
    monkeypatch.setattr(value, "_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(value, "_write", persist)
    monkeypatch.setattr(value, "_claude", lambda: {"available": True, "logged_in": True, "plan": "private-plan"})
    monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", lambda *_args, **_kwargs: ["model-live"])
    return value, config, calls


def test_login_is_native_scoped_cancellable_and_never_leaks_credentials(service):
    value, config, calls = service
    before = copy.deepcopy(config)
    assert "private" not in json.dumps(value.status())
    start = value.start("openai")
    assert start["flow"] == "device_code"
    assert "access_token" not in start
    with pytest.raises(ValueError, match="истёк"):
        value.poll("claude", start["sessionId"])
    assert value.poll("openai", start["sessionId"]) == {"sessionId": "own-session", "status": "approved"}
    value.cancel("openai", start["sessionId"])
    assert calls[-1] == ("/api/providers/oauth/sessions/own-session", None, "DELETE")
    assert config == before, "Connecting a subscription must not select a provider"
    assert value.models("openai") == {"models": [{"id": "model-live", "label": "model-live"}]}


@pytest.mark.parametrize("gateway_owned", [False, True])
def test_subscription_switch_and_return_use_native_home_without_a_prompt(service, gateway_owned):
    async def exercise():
        from websockets.asyncio.server import serve
        value, config, _ = service
        before = copy.deepcopy(config)
        value.auth.gateway_owned = gateway_owned
        received = []

        async def native(socket):
            async for raw in socket:
                request = json.loads(raw)
                received.append(request)
                result = {"session_id": "native-home", "messages": [{"text": "private-history"}]} if request["method"] == "session.resume" else {"deferred": True}
                await socket.send(json.dumps({"id": request["id"], "result": result}))

        async with serve(native, "127.0.0.1", 0) as server:
            value.auth.rpc_url = lambda: f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/ws"
            assert await value.select("claude", "opus-5.5", True) == {"saved": True, "deferred": True}
            assert config["model"] == {"provider": "claude-subscription-directsdk-experimental", "default": "opus-5.5"}
            assert config["fallback_providers"][0]["key_env"] == "AGENTSEE_INFERENCE_KEY"
            with pytest.raises(ValueError, match="Сначала выберите"):
                value.disconnect("claude")
            await value.select("managed", None, False)
        assert config["model"] == before["model"]
        for key in ("terminal", "agent", "stt"):
            assert config[key] == before[key]
        assert config["fallback_providers"] == []
        if gateway_owned:
            assert received == []
            assert config["autonomous"]["panel"]["model_selection_revision"]
        else:
            assert {row["method"] for row in received} == {"session.resume", "config.set"}
            assert all(row["params"]["session_id"] in {"agentready_home", "native-home"} for row in received)
            assert received[-1]["params"]["value"] == "managed-model --provider custom"
            resumes = [row['params'] for row in received if row['method'] == 'session.resume']
            assert all('source' in params and 'close_on_disconnect' in params for params in resumes)

    asyncio.run(exercise())


def test_managed_route_is_bound_to_the_customer_and_rejects_flags(service):
    value, config, _ = service
    original = copy.deepcopy(config)
    with pytest.raises(ValueError, match="корректный ID"):
        asyncio.run(value.select("openai", "model --provider custom", False))
    assert config == original
    config["model"]["base_url"] = "https://panel.invalid/app/api/agents/another-customer/inference/v1"
    with pytest.raises(ValueError, match="базовым инференсом"):
        value.status()


def test_claude_login_only_exports_the_official_authorization_url():
    from hermes_runtime.standalone.panel_claude_login import authorization_url
    assert authorization_url("private https://evil.invalid/oauth/authorize?token=private") is None
    assert authorization_url("https://claude.ai.evil.invalid/oauth/authorize?code=private") is None
    assert authorization_url("https://claude.ai/oauth/authorize?state=fixture") == "https://claude.ai/oauth/authorize?state=fixture"


def test_claude_code_owns_code_exchange_and_cancellation(tmp_path):
    import os
    import sys
    from hermes_runtime.standalone.panel_claude_login import ClaudeLogin
    child = tmp_path / "native_cli.py"
    child.write_text("import sys\nfrom pathlib import Path\nassert sys.argv[1:] == ['auth','login','--claudeai']\nprint('https://claude.ai/oauth/authorize?state=fixture',flush=True)\ncode=input()\nassert code=='fixture#state'\nPath('native-credential-fixture').write_text('native-owned')\n")
    login = ClaudeLogin([sys.executable, str(child)], os.environ, str(tmp_path))
    try:
        assert login.ready.wait(5)
        assert login.view()["url"] == "https://claude.ai/oauth/authorize?state=fixture"
        login.submit("fixture#state")
        login.process.wait(timeout=5)
        assert login.view() == {"status": "approved"}
        assert (tmp_path / "native-credential-fixture").read_text() == "native-owned"
    finally:
        login.cancel()
    abandoned = ClaudeLogin([sys.executable, str(child)], os.environ, str(tmp_path))
    assert abandoned.ready.wait(5)
    abandoned.cancel()
    assert abandoned.process.poll() is not None
    assert abandoned.view() == {"status": "cancelled"}
