"""Native gate, lineage and gateway authorization on real Hermes entry points."""

import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_managed_gate_is_home_scoped_and_native_mode_is_unchanged(
    tmp_path, monkeypatch
):
    from agentready_runtime.adapters import hermes
    from agentready_runtime.adapters.state import ensure_home_session
    from hermes_state import SessionDB
    from agent.context_compressor import ContextCompressor
    from hermes_constants import get_hermes_home

    from gateway.status import looks_like_gateway_command_line

    assert looks_like_gateway_command_line("/opt/hermes/.venv/bin/python -m agentready_runtime gateway run --no-supervise")
    assert not looks_like_gateway_command_line("python -m agentready_runtime gateway status")
    assert not looks_like_gateway_command_line("python -m agentready_runtime serve")
    assert not looks_like_gateway_command_line("python -m unrelated agentready_runtime gateway run")
    assert not looks_like_gateway_command_line("echo python -m agentready_runtime gateway run")
    home = Path(get_hermes_home())
    assert not hermes.enabled()
    sentinel = object()
    monkeypatch.setattr(
        ContextCompressor,
        "_agentready_native_generate_summary",
        lambda *a, **kw: sentinel,
    )
    compressor = ContextCompressor.__new__(ContextCompressor)
    assert compressor._generate_summary([]) is sentinel
    monkeypatch.setattr(hermes, "_home", home.resolve())
    ensure_home_session()
    db = SessionDB()
    db.create_session("unrelated", source="cli")
    assert db.resolve_resume_session_id("unrelated") == "unrelated"
    db.close()
    other = tmp_path / "other-home"
    other.mkdir()
    with monkeypatch.context() as scoped:
        scoped.setenv("HERMES_HOME", str(other))
        assert not hermes.enabled()
        assert compressor._generate_summary([]) is sentinel
        independent = SessionDB()
        assert independent.get_session("agentready_home") is None
        independent.close()
    assert hermes.enabled()


def test_busy_gateway_preserves_control_guard_and_event_envelope(tmp_path, monkeypatch):
    from agentready_runtime.adapters import hermes, gateway
    from gateway.run_busy import GatewayBusySessionMixin
    from hermes_constants import get_hermes_home
    import tools.approval

    monkeypatch.setattr(hermes, "_home", Path(get_hermes_home()).resolve())
    monkeypatch.setattr(tools.approval, "has_blocking_approval", lambda key: False)
    received = []
    monkeypatch.setattr(
        gateway,
        "forward_to_active_turn",
        lambda content: received.append(content) or True,
    )

    # Real native entry point, with I/O and authorization decision supplied by the host.
    class Host(GatewayBusySessionMixin):
        _draining = False
        allowed = True
        prepared = 0

        def _is_user_authorized_for_source(self, source):
            return self.allowed

        async def _agentready_native_handle_active_session_busy_message(
            self, *args, **kwargs
        ):
            return "native-control"

        async def _prepare_profile_scoped_inbound_message_text(self, **kwargs):
            self.prepared += 1
            return kwargs["event"].text

        def _consume_pending_native_image_paths(self, key):
            return []

    host = Host()
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="room",
        thread_id="topic",
        user_id="user",
    )
    event = SimpleNamespace(
        text="business event", message_id="42", source=source, internal=False
    )

    async def scenario():
        assert await host._handle_active_session_busy_message(event, "home") is True
        assert '"event_id": "telegram:42"' in received[0]
        host.allowed = False
        assert (
            await host._handle_active_session_busy_message(event, "home")
            == "native-control"
        )
        host.allowed = True
        event.text = "/stop"
        assert (
            await host._handle_active_session_busy_message(event, "home")
            == "native-control"
        )
        assert host.prepared == 1 and len(received) == 1

    asyncio.run(scenario())
