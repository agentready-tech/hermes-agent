"""AGE-6 T4: original cold native ingress is reply evidence across main compaction.

No cron job is created or bound; this covers reply proof, not timer cleanup.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from tests.tools.test_delegate_external_wait import _args, _call

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.mark.asyncio
async def test_native_main_reply_uses_original_admission_after_compaction(main_home, monkeypatch):
    from types import SimpleNamespace

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_runtime.sessions import HOME_SESSION_ID, home_session_key
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools.delegate_project_store import ProjectStore

    home, db = main_home
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions'))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.TELEGRAM: adapter}
    await initialize_session_authority(runner, profile_id=str(home), instance_id='offline-t4', db=db)
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    owner = SimpleNamespace(_session_db=db, session_id=HOME_SESSION_ID, _agentready_active_turn=SimpleNamespace())
    turns = []

    async def offline_turn(event, _source, key, _generation):
        sid = runner.session_store.peek_session_id(key)
        turns.append((event.message_id, key, sid))
        db.append_message(sid, 'user', event.text)
        db.append_message(sid, 'assistant', 'Acknowledged')
        return 'Acknowledged'

    monkeypatch.setattr(runner, '_handle_message_with_agent', offline_turn)

    async def deliver(message_id):
        # A transport replay retains the timestamp as well as the native ID.
        event = MessageEvent(text='Supplier confirms receipt', source=source, message_id=message_id,
                             timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        await asyncio.wait_for(adapter._message_handler(event), timeout=10)
        assert event._gateway_accepted
        return list_session_admissions(db, session_id=HOME_SESSION_ID, pending_only=False)

    earlier, = await deliver('101')
    assert (earlier['request_id'], earlier['status'], earlier['outcome']) == ('101', 'terminal', 'completed')
    with ProjectStore(owner) as store:
        project = store.create('order', 'Check supplier reply', '')['subagent_id']
    opened = _call(owner, **_args(project))
    assert opened['input_watermarks']['session_admissions'] == earlier['seq']
    assert opened['job_id'] is None

    child = 'compressed-main'
    db.publish_compression_child(
        parent_session_id=HOME_SESSION_ID, child_session_id=child, source='telegram',
        messages=[{'role': 'user', 'content': 'Summary of earlier work'}], require_compression_lease=False,
        watermark=0,
    )
    route = home_session_key()
    assert runner.session_store.advance_compression_session(route, HOME_SESSION_ID, child) is not None
    owner.session_id = runner.session_store.peek_session_id(route)
    assert owner.session_id == child
    assert any(row['content'] == earlier['payload']['text'] and row['id'] > opened['input_watermarks']['messages']
               for row in db.get_messages(child))

    def close(row):
        return _call(owner, op='close', project_id=project, wait_id=opened['wait_id'], reason='reply',
                     reply_ref={'kind': 'session_admission', 'admission_id': row['admission_id']})

    replayed, = await deliver('101')
    assert replayed['admission_id'] == earlier['admission_id']
    assert close(replayed).get('error')
    assert _call(owner, op='select', project_id=project, wait_id=opened['wait_id']) == opened

    _, later = await deliver('102')
    assert (later['request_id'], later['status'], later['outcome']) == ('102', 'terminal', 'completed')
    assert later['seq'] > opened['input_watermarks']['session_admissions']
    assert later['admission_id'] != earlier['admission_id']
    assert turns == [('101', route, HOME_SESSION_ID), ('102', route, child)]
    closed = close(later)
    assert closed.pop('success') is True
    assert closed.pop('project_id') == project
    assert (closed['state'], closed['close_reason'], closed['job_id']) == ('CLOSED', 'reply', None)
    with ProjectStore(owner) as store:
        assert store.read_workspace(project)['external_wait'] == closed
    with db._read_ctx() as conn:
        assert {row[0] for row in conn.execute('SELECT id FROM sessions')} == {HOME_SESSION_ID, child}
