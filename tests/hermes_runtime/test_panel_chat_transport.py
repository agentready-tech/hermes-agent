"""Panel chat transports the existing native RPC and home events without a second chat backend."""

import asyncio
import json
import pytest

from hermes_runtime.standalone.panel import bridge
from hermes_runtime.standalone.panel_auth import NativeScreenAuth


def test_chat_reverse_channel_preserves_native_rpc_and_observer_feed():
    async def exercise():
        from websockets.asyncio.server import serve
        rpc_received = asyncio.Event()
        received = []
        observed = []

        async def native(socket):
            if socket.request.path.startswith("/api/events?"):
                assert "channel=agentready_home" in socket.request.path
                await rpc_received.wait()
                await socket.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "status.update", "session_id": "native-home", "payload": {"status": "idle"}}}))
                await socket.wait_closed()
                return
            assert socket.request.path.startswith("/api/ws?")
            async for raw in socket:
                frame = json.loads(raw)
                received.append(frame)
                await socket.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"session_id": "native-home", "messages": []}}))
                rpc_received.set()

        async def panel(socket):
            assert socket.request.headers["Authorization"] == "Bearer fixture-machine-key"
            await socket.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.resume", "params": {"session_id": "agentready_home"}}))
            while len(observed) < 2:
                observed.append(json.loads(await socket.recv()))
            await socket.close(code=1000)

        async with serve(native, "127.0.0.1", 0) as native_server, serve(panel, "127.0.0.1", 0) as panel_server:
            native_port = native_server.sockets[0].getsockname()[1]
            panel_port = panel_server.sockets[0].getsockname()[1]
            await asyncio.wait_for(bridge({"kind": "chat", "id": "fixture-reverse"},
                {"url": f"http://127.0.0.1:{panel_port}", "environment_id": "env_fixture"},
                native_port, "fixture-machine-key", NativeScreenAuth(native_port, "fixture-native-key")), timeout=10)
        assert received == [{"jsonrpc": "2.0", "id": 1, "method": "session.resume", "params": {"session_id": "agentready_home"}}]
        assert any(frame.get("result", {}).get("session_id") == "native-home" for frame in observed)
        assert any(frame.get("params", {}).get("type") == "status.update" for frame in observed)

    asyncio.run(exercise())


@pytest.mark.parametrize("dashboard", [True, False])
def test_panel_worker_binds_its_home_before_choosing_chat_transport(monkeypatch, tmp_path, dashboard):
    from hermes_runtime.adapters import hermes, state
    from hermes_runtime.standalone import panel, config
    import agent.secret_scope
    monkeypatch.setattr(hermes, "_home", None)
    monkeypatch.setattr(config, "settings", lambda home: {
        "dashboard": {"enabled": dashboard}, "port": 1,
        "panel": {"url": "https://panel.invalid", "environment_id": "env_fixture"}})
    monkeypatch.setattr(agent.secret_scope, "get_secret", lambda name: "fixture-credential")
    monkeypatch.setattr(state, "main_gateway_origin", lambda: {"session_key": "agent:main:agentready:home"})

    async def connected(settings, port, credential, auth, subscriptions):
        from hermes_runtime.standalone.panel_chat import gateway_chat_available
        assert hermes.enabled(), "Panel's own worker must activate the managed home"
        assert hermes.managed_home() == tmp_path.resolve()
        assert gateway_chat_available(), "Panel must choose the existing gateway, not the legacy writer"
        raise asyncio.CancelledError

    monkeypatch.setattr(panel, "connection", connected)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(panel.worker(tmp_path))
