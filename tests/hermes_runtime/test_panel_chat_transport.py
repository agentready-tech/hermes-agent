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


def test_new_employee_admits_one_hidden_introduction_without_a_viewer(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hermes_cli import gateway_client, gateway_runtime
    from hermes_runtime.standalone.panel_chat import introduce_employee

    client = SimpleNamespace(rpc=AsyncMock(side_effect=[{}, OSError("lost ACK"), {}, {}]))
    endpoint = object()
    monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint",
                        lambda home: SimpleNamespace(state="ready", endpoint=endpoint))

    @asynccontextmanager
    async def connect(target):
        assert target is endpoint
        yield client

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    panel = {"environment_id": "env_fixture"}
    asyncio.run(introduce_employee(tmp_path, panel))
    client.rpc.assert_not_awaited()

    panel["introduction_locale"] = "ru"
    asyncio.run(introduce_employee(tmp_path, panel))
    asyncio.run(introduce_employee(tmp_path, panel))
    resume, submit, retry_resume, retry_submit = client.rpc.await_args_list
    assert (retry_resume, retry_submit) == (resume, submit)
    assert resume.args == ("session.resume",)
    assert submit.args == ("prompt.submit",)
    assert submit.kwargs["session_id"] == resume.kwargs["session_id"] == "agentready_home"
    assert submit.kwargs["submission_id"] == "agentsee-introduction:env_fixture"
    assert submit.kwargs["display_kind"] == "hidden"
    assert "in Russian" in submit.kwargs["text"]


@pytest.mark.parametrize("dashboard", [True, False])
@pytest.mark.parametrize("backend", ["kubernetes", "firecracker"])
def test_panel_worker_binds_its_home_before_choosing_chat_transport(monkeypatch, tmp_path, dashboard, backend):
    from hermes_runtime.adapters import hermes, state
    from hermes_runtime.standalone import computer, panel, config
    from tools.bot_desktop import placement
    desktop_starts = []
    monkeypatch.setattr(computer, "start_desktop", lambda: desktop_starts.append(backend))
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, backend))
    monkeypatch.setattr(hermes, "_home", None)
    monkeypatch.setattr(config, "settings", lambda home: {
        "dashboard": {"enabled": dashboard}, **({"port": 1} if dashboard else {}),
        "computer": {"enabled": True},
        "panel": {"url": "https://panel.invalid", "environment_id": "env_fixture"}})
    monkeypatch.setenv("HERMES_PANEL_CREDENTIAL", "fixture-credential")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "fixture-token")
    monkeypatch.setattr(state, "main_gateway_origin", lambda: {"session_key": "agent:main:agentready:home"})

    async def connected(settings, port, credential, auth, subscriptions, google_auth, skills):
        assert port == (1 if dashboard else None)
        assert skills.home == tmp_path.resolve() and skills.auth.gateway_owned
        assert google_auth.home == tmp_path.resolve() and google_auth.gateway_owned
        from hermes_runtime.standalone.panel_chat import gateway_chat_available
        assert hermes.enabled(), "Panel's own worker must activate the managed home"
        assert hermes.managed_home() == tmp_path.resolve()
        assert gateway_chat_available(), "Panel must choose the existing gateway, not the legacy writer"
        raise asyncio.CancelledError

    monkeypatch.setattr(panel, "connection", connected)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(panel.worker(tmp_path))
    assert desktop_starts == ([backend] if backend == "kubernetes" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [None, False, True, 1, "true"])
async def test_skills_open_transmits_only_the_trusted_system_permission(tmp_path, permission):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    skills = SimpleNamespace(available=lambda: True, bridge=AsyncMock())
    command = {"kind": "skills", "id": "fixture-skill-read", "canReadSystemSkills": permission}
    await bridge(command, {"url": "https://panel.invalid", "environment_id": "env_fixture"},
                 1, "synthetic-credential", None, skills=skills)
    assert skills.bridge.await_args.kwargs == {
        "can_read_system": permission is True,
        "can_edit_personal": False,
    }
