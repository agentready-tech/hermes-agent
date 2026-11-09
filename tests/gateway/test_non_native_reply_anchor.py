"""A Panel/local admission ID is durable turn identity, never a Telegram reply anchor."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('thread', [None, '7'])
async def test_non_native_admission_on_messaging_main_delivers_without_recovery(monkeypatch, thread):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.helpers import _reply_anchor_for_event
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_contract import Principal, Submission
    from gateway.session_ingress import deliver_response, execute_admission
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import get_session_admission
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda *_: None)
    runner = GatewayRunner(GatewayConfig())
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=456))
    adapter._rich_send_disabled = True
    adapter._reply_to_mode = 'all'
    runner.adapters = {Platform.TELEGRAM: adapter}
    adapter.gateway_runner = runner
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='anchor-fixture')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id=thread)
    source.message_id = '999'  # a previous native trigger must not become this local reply's anchor
    source_before = source.to_dict()
    native = MessageEvent(text='native fixture', source=source, message_id='123')
    with native_callback(runner, native, get_hermes_home()):
        receipt = await authority.admit_native(native)
    authority.db.append_message(receipt.ref.session_id, 'user', 'existing fixture question')
    authority.db.append_message(receipt.ref.session_id, 'assistant', 'existing fixture answer')
    history = authority.db.get_messages(receipt.ref.session_id)
    actor = Principal('42', authority.profile_id, frozenset({'session:submit', 'session:read'}), 'local-fixture')
    local = await authority.submit(actor, Submission('local-fixture', receipt.ref,
                                                    {'text': 'ordinary fixture'}, 'queue'))
    row = get_session_admission(authority.db, admission_id=local.admission_id)
    seen = []

    async def inference(event):
        seen.append(event)
        return 'fixture response'

    monkeypatch.setattr(runner, '_handle_message', inference)
    try:
        await execute_admission(authority, receipt.ref, row)
        [event] = seen
        assert event.message_id == row['admission_id'], 'keep canonical input identity'
        assert event.source.to_dict() == source_before
        assert not event.internal, 'a user submission retains normal authorization semantics'
        assert _reply_anchor_for_event(event) is None, 'an admission UUID is not a platform message'
        await deliver_response(adapter, event, authority.sessions[receipt.ref.session_id].route,
                               'fixture response')
        adapter._bot.send_message.assert_awaited_once()
        assert adapter._bot.send_message.call_args.kwargs.get('reply_to_message_id') is None
        if thread is not None:
            assert adapter._bot.send_message.call_args.kwargs['message_thread_id'] == int(thread)
        with authority.db._read_ctx() as conn:
            rows = conn.execute('SELECT state,attempts FROM delivery_obligations').fetchall()
        assert [tuple(value) for value in rows] == [('delivered', 0)]
        assert authority.db.get_messages(receipt.ref.session_id) == history
    finally:
        runner.session_store.close_all_db_handles()
        authority.db.close()


def test_anchorless_admission_keeps_topic_but_native_internal_and_redirect_contracts():
    from gateway.config import Platform
    from gateway.platforms.helpers import _reply_anchor_for_event, _thread_metadata_for_event
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from plugins.platforms.telegram.adapter import TelegramAdapter

    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42', chat_type='dm',
                           thread_id='7', message_id='999')
    admitted = MessageEvent(text='fixture', source=source, message_id='a' * 32, reply_anchor_override='')
    metadata = _thread_metadata_for_event(admitted)
    assert _reply_anchor_for_event(admitted) is None
    assert metadata['thread_id'] == '7'
    assert 'telegram_reply_to_message_id' not in metadata, 'do not revive a stale source anchor'
    assert TelegramAdapter._reply_to_message_id_for_send(None, metadata) is None
    native = MessageEvent(text='fixture', source=source, message_id='123')
    assert _reply_anchor_for_event(native) == '123'
    assert _thread_metadata_for_event(native)['telegram_reply_to_message_id'] == '123'
    native.internal = True
    assert _reply_anchor_for_event(native) is None
    native.reply_anchor_override = '456'
    assert _reply_anchor_for_event(native) == '456'
