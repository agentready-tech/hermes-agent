"""Exercise standalone apply over the actual native session.resume/config.set handlers."""

import asyncio
import json

import pytest

from hermes_runtime.standalone.panel_subscriptions import SubscriptionError
from providers.agy_policy import MODEL, PROVIDER
from tests.hermes_runtime.test_panel_agy_login import VIEWER, setup


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
@pytest.mark.parametrize("case", ["applied", "stale-revision", "switch-conflict", "foreign-session", "busy", "busy-stale"])
async def test_native_subscription_apply_and_receipt(tmp_path, monkeypatch, case):
    from hermes_cli.config import set_config_value
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.workspace.transport import profile_scope
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tui_gateway import server
    from websockets.asyncio.server import serve

    a, _, store, _ = setup(tmp_path, monkeypatch)
    assert store.promote(a.home, store.stage(a.home))
    a.auth.gateway_owned = False
    default_ctx = {"quiet_mode": True, "skip_context_files": True, "skip_memory": True, "enabled_toolsets": []}
    monkeypatch.setenv("HERMES_HOME", str(a.home))
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_: None)  # No long-lived background worker in this fixture.
    main_sid, foreign_sid = "standalone-live-fixture", "standalone-other-fixture"
    with SessionDB(a.home / "state.db") as db, profile_scope(a.home):
        db.create_session(HOME_SESSION_ID, source="agentready", model=MODEL)
        db.create_session("neighbor-fixture", source="agentready", model=MODEL)
        monkeypatch.setattr(server, "_get_db", lambda: db)
        agent = AIAgent(provider=PROVIDER, model=MODEL, session_id=HOME_SESSION_ID, session_db=db, **default_ctx)
        server._init_session(main_sid, HOME_SESSION_ID, agent, [], cwd=str(a.home), source="agentready", session_db=db)
        own = server._sessions[main_sid]
        previous_client = agent.client
        monkeypatch.setitem(server._sessions, foreign_sid, {**own, "session_key": "neighbor-fixture"})
        own["running"] = busy = case.startswith("busy")
        seen = []
        if case == "switch-conflict":
            from hermes_cli import model_switch
            real = model_switch.switch_model

            def resolve_then_write(*args, **kwargs):
                resolved = real(*args, **kwargs)
                set_config_value("model.default", "fixture-newer-model")
                return resolved

            monkeypatch.setattr(model_switch, "switch_model", resolve_then_write)

        async def native(socket):
            async for raw in socket:
                request = json.loads(raw)
                if request["method"] == "config.set":
                    if case == "stale-revision":
                        # The ordinary config writer wins in the gap between Panel's precheck and native apply.
                        await asyncio.to_thread(set_config_value, "model.default", "fixture-newer-model")
                    if case == "foreign-session":
                        request["params"]["session_id"] = foreign_sid
                response = await asyncio.to_thread(server.handle_request, request)
                seen.append((request, response))
                await socket.send(json.dumps(response))
                if case == "applied" or busy:
                    assert "error" not in response, response

        async def select():
            return await a.handle("subscriptions.select", {"provider": "gemini", "model": MODEL,
                                  "managedFallback": False}, viewer=VIEWER)

        try:
            async with serve(native, "127.0.0.1", 0) as socket:
                a.auth.rpc_connection = lambda: (f"ws://127.0.0.1:{socket.sockets[0].getsockname()[1]}", {})
                if case in {"stale-revision", "switch-conflict", "foreign-session"}:
                    with pytest.raises(SubscriptionError, match="не переключён"):
                        await select()
                    assert "error" in seen[-1][1] and own.get("model_override") is None
                    assert agent.client is previous_client and not store.children
                    expected = MODEL if case == "foreign-session" else "fixture-newer-model"
                    assert a._config()["model"]["default"] == expected
                    assert db.get_messages("neighbor-fixture") == []
                    return
                saved = await select()
            assert [req["method"] for req, _ in seen] == ["session.resume", "config.set"]
            assert seen[0][1]["result"]["session_key"] == HOME_SESSION_ID
            native_receipt = seen[1][1]["result"]["panel_selection"]
            assert native_receipt == {"session_id": main_sid, "session_key": HOME_SESSION_ID,
                "revision": saved["revision"], "generation": store.active(a.home), "provider": PROVIDER, "model": MODEL}
            assert saved == {"saved": True, "deferred": busy, "revision": native_receipt["revision"]}
            assert not store.children  # Selecting never starts inference.
            if busy:
                assert agent.client is previous_client and "pending_model_switch" in own
                if case == "busy-stale":
                    await asyncio.to_thread(set_config_value, "model.default", "fixture-newer-model")
                own["running"] = False
                await asyncio.to_thread(server._apply_pending_model_switch, main_sid, own)
                assert "pending_model_switch" not in own
                if case == "busy-stale":
                    assert agent.client is previous_client and own.get("model_override") is None
                    assert a._config()["model"]["default"] == "fixture-newer-model"
                    assert not store.children
                    return
            assert agent.client is not previous_client
            assert (agent.provider, agent.model, agent.client.generation) == (PROVIDER, MODEL, native_receipt["generation"])
            result = await asyncio.to_thread(agent.run_conversation, "Inert standalone request")
            assert result["final_response"] == "fixed fixture reply"
            assert [op for op, _, _ in store.children] == ["inference"]
            with SessionDB(a.home / "state.db") as reopened:
                assert reopened.get_messages(HOME_SESSION_ID)[-1]["content"] == "fixed fixture reply"
                assert reopened.get_messages("neighbor-fixture") == []
        finally:
            server._sessions.pop(foreign_sid, None)
            server.handle_request({"id": 3, "method": "session.close", "params": {"session_id": main_sid}})
            agent.close()


@pytest.mark.asyncio
async def test_panel_and_native_writer_share_read_modify_write_lock(tmp_path, monkeypatch):
    from threading import Event
    from hermes_cli import config
    from hermes_runtime.workspace.transport import profile_scope

    service, _, store, _ = setup(tmp_path, monkeypatch)
    assert store.promote(service.home, store.stage(service.home))
    write_ready, writer_entered, writer_done = Event(), Event(), Event()
    original = service._write

    def paused_panel_write(data):
        write_ready.set()  # Panel has read, edited, and still owns the physical config lock.
        assert writer_entered.wait(5)
        assert not writer_done.is_set()
        original(data)

    def native_writer():
        assert write_ready.wait(5)
        writer_entered.set()
        with profile_scope(service.home):
            config.set_config_value("agent.max_turns", "77")
        writer_done.set()

    monkeypatch.setattr(service, "_write", paused_panel_write)
    await asyncio.gather(asyncio.to_thread(native_writer),
        service.handle("subscriptions.select", {"provider": "gemini", "model": MODEL, "managedFallback": False}, viewer=VIEWER))
    raw = service._config()
    assert writer_done.is_set()
    assert raw["agent"]["max_turns"] == 77 and raw["model"] == {"provider": PROVIDER, "default": MODEL}
    assert not store.children
