"""Ordinary managed home events never cancel the writer, regardless of legacy busy settings."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def busy_home(monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.adapters.turns import ActiveHomeTurn
    from hermes_runtime.sessions import HOME_SESSION_KEY

    with tempfile.TemporaryDirectory(prefix="hgb-") as directory:
        home = Path(directory)
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(hermes, "_home", home.resolve())
        ensure_home_session()
        turn = ActiveHomeTurn()
        host = GatewayRunner(config=GatewayConfig())
        receiver = SimpleNamespace(interrupt=Mock(), redirect=Mock(return_value=False), steer=Mock(return_value=False),
            _supports_active_turn_redirect=True)
        host._session_state(HOME_SESSION_KEY).turn.agent = receiver
        host._delivery_adapter_for = lambda _: SimpleNamespace(send=AsyncMock())
        host._is_user_authorized_for_source = lambda _: True
        host._admit_bot_message_for_source = lambda _: True
        host._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="new event")
        host._consume_pending_native_image_paths = lambda _: []
        host._agent_has_active_subagents = lambda _: False
        host._session_has_compression_in_flight = AsyncMock(return_value=False)
        host._pending_event_audio_paths = lambda _: []
        host._queue_or_replace_pending_event = Mock()
        host._send_busy_ack_reply = AsyncMock()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm")
        try:
            yield host, receiver, turn, source, HOME_SESSION_KEY
        finally:
            turn.finish(error="test finished")
            from hermes_logging import _reset_queued_handlers
            _reset_queued_handlers()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["authority", "priority", "adapter"])
@pytest.mark.parametrize("kind", ["text", "voice", "photo"])
@pytest.mark.parametrize("legacy_mode", ["interrupt", "queue", "steer"])
async def test_busy_event_joins_durable_home_without_interrupt_or_redirect(busy_home, entry, kind, legacy_mode):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_runtime.adapters.inbox import HomeInbox

    host, receiver, turn, source, key = busy_home
    host._busy_input_mode = legacy_mode
    host._busy_text_mode = "queue" if legacy_mode == "queue" else "interrupt"
    event = MessageEvent(text="new event", message_type=MessageType(kind), source=source,
        message_id="event-42", media_urls=[] if kind == "text" else ["/untrusted/attachment"])
    if entry == "authority":
        assert await host._hm_busy_preempt(event, source, key)
    elif entry == "priority":
        await host._hm_handle_running_session_message(event, source, key)
    else:
        assert await host._handle_active_session_busy_message(event, key)
    receiver.interrupt.assert_not_called()
    receiver.redirect.assert_not_called()
    receiver.steer.assert_not_called()
    host._send_busy_ack_reply.assert_not_called()
    host._queue_or_replace_pending_event.assert_not_called()
    content = turn.take()
    assert len(content) == 1 and '"event_id": "telegram:event-42"' in content[0]
    assert HomeInbox().pending()[0][1] == content[0]
    # Redelivery of the same platform event cannot create another admission.
    assert await host._hm_busy_preempt(event, source, key)
    assert turn.take() == []
    assert len(HomeInbox().pending()) == 1


@pytest.mark.asyncio
async def test_busy_event_without_active_mailbox_is_queued_without_cancel(busy_home, monkeypatch):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_runtime.adapters import gateway

    host, receiver, _, source, key = busy_home
    event = MessageEvent(text="caption", message_type=MessageType.PHOTO, source=source, media_urls=["image"])
    monkeypatch.setattr(gateway, "forward_to_active_turn", lambda *_: None)
    assert await host._hm_busy_preempt(event, source, key) is False
    await host._hm_handle_running_session_message(event, source, key)
    host._queue_or_replace_pending_event.assert_called_once_with(key, event)
    receiver.interrupt.assert_not_called()
    receiver.redirect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/stop", "/approve", "/deny"])
async def test_explicit_control_stays_with_native_handler(busy_home, command):
    from gateway.platforms.event import MessageEvent
    from hermes_runtime.adapters.gateway import busy_event

    host, _, turn, source, key = busy_home
    proceed = AsyncMock(return_value=True)
    event = MessageEvent(text=command, source=source)
    assert await busy_event(host, proceed, event, key)
    proceed.assert_awaited_once_with(event, key)
    assert turn.take() == []


@pytest.mark.asyncio
async def test_busy_photo_keeps_image_in_durable_inbox(busy_home, tmp_path):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_runtime.adapters.inbox import HomeInbox

    host, receiver, turn, source, key = busy_home
    image = tmp_path / "photo.png"
    image.write_bytes(b"test image bytes")
    host._consume_pending_native_image_paths = lambda _: [str(image)]
    event = MessageEvent(text="look at this", message_type=MessageType.PHOTO, source=source, message_id="photo-1")
    await host._hm_handle_running_session_message(event, source, key)
    content, = turn.take()
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert HomeInbox().pending()[0][1] == content
    receiver.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_session_retains_native_busy_policy(busy_home):
    from gateway.platforms.event import MessageEvent
    from hermes_runtime.adapters.gateway import busy_event

    host, _, turn, source, _ = busy_home
    proceed = AsyncMock(return_value=True)
    event = MessageEvent(text="native message", source=source)
    key = "agent:other:telegram:42"
    assert await busy_event(host, proceed, event, key)
    proceed.assert_awaited_once_with(event, key)
    assert turn.take() == []


@pytest.mark.asyncio
async def test_unrecognized_slash_text_is_an_event_not_a_cancel(busy_home):
    from gateway.platforms.event import MessageEvent

    host, receiver, turn, source, key = busy_home
    host._busy_input_mode = "interrupt"
    event = MessageEvent(text="/a/path/to/a/file", source=source, message_id="path-42")
    await host._hm_handle_running_session_message(event, source, key)
    assert len(turn.take()) == 1
    receiver.interrupt.assert_not_called()
