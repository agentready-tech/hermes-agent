"""AGE-11: scoped subscription RPC to the original main agent through fixture processes."""

import asyncio
import json

import pytest

pytest_plugins = ["tests.hermes_runtime.test_main_schedule_tools"]


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
async def test_scoped_wire_promotion_and_main_turn_readback(main_home, monkeypatch, tmp_path):
    from gateway import run
    from gateway.config import GatewayConfig
    from hermes_runtime.adapters.main_model import resolve_turn_runtime
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    from hermes_runtime.standalone import agy_runtime
    from hermes_runtime.standalone.panel_agy_login import Attempt, GeminiSubscriptions
    from hermes_runtime.standalone.panel_subscriptions import Subscriptions
    from hermes_state import SessionDB
    from providers.agy_policy import MODEL, PROVIDER
    from run_agent import AIAgent
    from tests.hermes_runtime.test_panel_agy_login import Auth, FixtureAuthority, FIXTURE_URL, VIEWER
    from tests.hermes_runtime.test_panel_main_bootstrap import runtime
    from tests.waiting import async_wait_until
    from websockets.asyncio.server import serve

    home, db = main_home
    config_path = home / "config.yaml"
    config_path.write_text(json.dumps({
        "model": {"provider": "custom", "default": "managed-fixture", "key_env": "AGENTSEE_INFERENCE_KEY",
                  "base_url": "https://panel.fixture.invalid/app/api/agents/A/inference/v1"},
        "autonomous": {"panel": {"url": "https://panel.fixture.invalid", "environment_id": "A"}},
        "compression": {"enabled": False},
    }))
    store = FixtureAuthority(home)
    store.binding = tmp_path / "A-fixture-store.json"
    monkeypatch.setattr(agy_runtime, "admit", lambda owner, _op: store if owner == home else None)
    service = Subscriptions(home, Auth(), gemini=GeminiSubscriptions(home, attempt_factory=lambda *args: Attempt(
        *args, validate_url=lambda url: url == FIXTURE_URL, timeout=5)))
    monkeypatch.setattr(service, "_claude", dict)
    authority, connection = runtime(home, db, monkeypatch)
    assert "result" in await connection.dispatch({"id": 0, "method": "session.resume", "params": {"session_id": HOME_SESSION_ID}})
    runner = run.GatewayRunner(GatewayConfig())
    runner.session_store = authority.runner.session_store
    source = authority.sessions[HOME_SESSION_ID].source
    saved = {}
    done = asyncio.Event()

    async def panel(socket):
        counter = 0
        async def request(method, **params):
            nonlocal counter
            counter += 1
            await socket.send(json.dumps({"jsonrpc": "2.0", "id": counter, "method": f"subscriptions.{method}", "params": {
                "provider": "gemini", **params}}))
            reply = json.loads(await socket.recv())
            assert "error" not in reply and reply["id"] == counter
            return reply["result"]
        try:
            sid = (await request("login.start"))["sessionId"]
            attempt = service.gemini.attempt
            assert await async_wait_until(lambda: attempt.view().get("url") == FIXTURE_URL, timeout=3)
            assert (await request("login.poll", sessionId=sid))["url"] == FIXTURE_URL
            assert await request("login.code", sessionId=sid, code="fixture-only") == {"submitted": True}
            assert await asyncio.to_thread(attempt.done.wait, 3)
            assert (await request("login.poll", sessionId=sid))["status"] == "approved"
            saved.update(await request("select", model=MODEL, managedFallback=False))
        finally:
            done.set()
            await socket.close()

    try:
        async with serve(panel, "127.0.0.1", 0) as listener:
            address = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            await asyncio.wait_for(service.bridge(address, "fixture-connector", viewer=VIEWER), 8)
            assert done.is_set() and saved.get("deferred")
        route_config = run._load_gateway_config(config_path)
        assert route_config["model"] == {"provider": PROVIDER, "default": MODEL}
        model, kwargs = resolve_turn_runtime(runner, source, HOME_SESSION_KEY, route_config)
        assert model == MODEL and kwargs["provider"] == PROVIDER
        assert runner.session_store._entries[HOME_SESSION_KEY].metadata["panel_model_selection_revision"] == saved["revision"]
        agent = AIAgent(model=model, session_id=HOME_SESSION_ID, session_db=db, quiet_mode=True, skip_memory=True,
                        skip_context_files=True, enabled_toolsets=[], max_iterations=2, **kwargs)
        try:
            history = db.get_messages_as_conversation(HOME_SESSION_ID)
            result = await asyncio.to_thread(agent.run_conversation, "Inert fixture only", conversation_history=history)
            assert result["final_response"] == "fixed fixture reply"
        finally:
            agent.close()
        with SessionDB(home / "state.db") as readback:
            assert readback.get_messages(HOME_SESSION_ID)[-1]["content"] == "fixed fixture reply"
        assert [op for op, _, _ in store.children] == ["login", "probe", "inference"]
        assert {gen for _, gen, _ in store.children} == {store.active(home)}
        assert len({child.pid for _, _, child in store.children}) == 3
    finally:
        service.gemini.retire()
        await connection.close()
        runner.session_store.close_all_db_handles()
