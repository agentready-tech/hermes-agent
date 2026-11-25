"""AGE-11: scoped subscription RPC to the original main agent through fixture processes."""

import asyncio
import json
from pathlib import Path

import pytest

pytest_plugins = ["tests.hermes_runtime.test_main_schedule_tools"]


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
@pytest.mark.parametrize("ingress", ["wire", "panel"])
async def test_scoped_wire_promotion_and_main_turn_readback(main_home, monkeypatch, tmp_path, ingress):
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

    if ingress == "panel" and not _panel_script().is_file():
        pytest.skip("cross-repository proof requires the sibling AGE-11 AgentReady checkout")
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
        if ingress == "panel":
            await _component_through_relay(service)
            revision = service._config()["autonomous"]["panel"]["model_selection_revision"]
        else:
            async with serve(panel, "127.0.0.1", 0) as listener:
                address = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
                await asyncio.wait_for(service.bridge(address, "fixture-connector", viewer=VIEWER), 8)
                assert done.is_set() and saved.get("deferred")
            revision = saved["revision"]
        route_config = run._load_gateway_config(config_path)
        assert route_config["model"] == {"provider": PROVIDER, "default": MODEL}
        model, kwargs = resolve_turn_runtime(runner, source, HOME_SESSION_KEY, route_config)
        assert model == MODEL and kwargs["provider"] == PROVIDER
        assert runner.session_store._entries[HOME_SESSION_KEY].metadata["panel_model_selection_revision"] == revision
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


def _panel_script():
    return (Path(__file__).resolve().parents[3] / "AgentReady" / "apps/control-plane/scripts"
            / "subscriptions-hermes-loopback.mjs")


async def _component_through_relay(service):
    from contextlib import suppress
    from websockets.exceptions import ConnectionClosed
    from hermes_runtime.standalone.panel import connection

    script = _panel_script()
    child = await asyncio.create_subprocess_exec("node", str(script), cwd=script.parent.parent,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output = []
    connector = None

    async def line_with(prefix):
        while line := await child.stdout.readline():
            decoded = line.decode("utf-8", "replace").strip()
            output.append(decoded)
            if prefix in decoded:
                return decoded.split(prefix, 1)[1]
        pytest.fail("Panel fixture did not reach its milestone:\n" + "\n".join(output))

    try:
        async with asyncio.timeout(20):
            port = int(await line_with("AGE11_LOOPBACK_PORT="))
            connector = asyncio.create_task(connection({"url": f"http://127.0.0.1:{port}", "environment_id": "A"},
                None, "fixture-connector", service.auth, subscriptions=service))
            await line_with("AGE11_AGENT_AUTH")
            child.stdin.write(b"ready\n")
            await child.stdin.drain()
            child.stdin.close()
            await line_with("AGE11_VIEW_SELECTED")
            assert await child.wait() == 0, "\n".join(output) + (await child.stdout.read()).decode()
    finally:
        if connector is not None:
            connector.cancel()
            with suppress(asyncio.CancelledError, ConnectionClosed):
                await connector
        if child.returncode is None:
            child.kill()
            await child.wait()
