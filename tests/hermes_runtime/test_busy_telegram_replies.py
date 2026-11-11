"""Managed Telegram keeps the native FIFO and delivery boundary for each input."""

import asyncio
import json
import threading
from contextlib import closing
from dataclasses import replace
from http.server import ThreadingHTTPServer
import sqlite3

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from tests.hermes_runtime.test_standalone_service_e2e import Provider
from tests.waiting import async_wait_until


class _Telegram(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token='local-fixture'), Platform.TELEGRAM)
        self.replies = []
        self.refuse = None

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if reply_to is not None and reply_to == self.refuse:
            return SendResult(success=False, error='fixture transport refused send')
        self.replies.append((chat_id, content, reply_to, (metadata or {}).get('thread_id')))
        return SendResult(success=True, message_id=str(len(self.replies) + 100))

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {'id': chat_id}


@pytest.mark.asyncio
@pytest.mark.parametrize('followup', ['ordinary', 'recover-follower', 'explicit-steer'])
async def test_busy_telegram_reply_boundary(tmp_path, monkeypatch, followup):
    from gateway import delivery_ledger
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_runtime.adapters import hermes
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.config import activate_home

    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', tmp_path / 'state.db')
    monkeypatch.setattr(delivery_ledger, '_db_path', lambda: tmp_path / 'state.db')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    provider.calls = []
    provider.block_next = True
    provider.entered, provider.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{provider.server_port}/v1'
    monkeypatch.setenv('OPENAI_API_KEY', 'loopback-only')
    monkeypatch.setenv('OPENAI_BASE_URL', base)
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    (tmp_path / '.env').write_text('OPENAI_API_KEY=loopback-only\nTELEGRAM_ALLOWED_USERS=42\nTELEGRAM_HOME_CHANNEL=42\n')
    (tmp_path / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'gpt-4.1', 'base_url': base, 'context_length': 128000},
        'terminal': {'cwd': str(tmp_path)}, 'compression': {'enabled': False},
        'streaming': {'enabled': False}, 'auxiliary': {'title_generation': {'enabled': False}},
    }))
    activate_home(tmp_path)
    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='busy-replies')
    adapter = _Telegram()
    adapter.refuse = '102' if followup == 'recover-follower' else None
    adapter.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='9')
    first = MessageEvent(text='Delivery Friday', source=source, message_id='101')
    second_text = 'Check the separate order'
    if followup == 'explicit-steer':
        second_text = '/steer ' + second_text
    second = MessageEvent(text=second_text, source=source, message_id='102')

    def rows(sql):
        with closing(sqlite3.connect(tmp_path / 'state.db')) as db:
            return db.execute(sql).fetchall()

    try:
        await adapter.handle_message(first)
        await async_wait_until(provider.entered.is_set, timeout=20, what='first model call')
        await asyncio.wait_for(adapter.handle_message(second), timeout=5)
        before = rows('SELECT request_id, target_session_id, status FROM session_admissions ORDER BY seq')
        provider.release.set()
        await async_wait_until(lambda: len(provider.calls) == 2 and authority.sessions[HOME_SESSION_ID].task.done(),
                               timeout=20, what='two completed turns')
        if adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(adapter._background_tasks)), timeout=10)
        text = 'Delivery reviewed in the same main session.'
        if followup == 'explicit-steer':
            assert before == [('101', HOME_SESSION_ID, 'started')]
            assert any('Check the separate order' in json.dumps(call) for call in provider.calls)
            assert [item for item in adapter.replies if item[1] == text] == [('42', text, '101', '9')]
            return

        assert before == [('101', HOME_SESSION_ID, 'started'), ('102', HOME_SESSION_ID, 'queued')]
        expected_replies = [('42', text, '101', '9'), ('42', text, '102', '9')]
        if followup == 'recover-follower':
            assert adapter.replies == expected_replies[:1]
            # The boot sweep claims only the failed obligation from a previous owner.
            adapter.refuse = None
            monkeypatch.setattr(delivery_ledger, '_owner_alive', lambda *_: False)
            assert await runner._redeliver_pending_obligations() == 1
            expected_replies[1] = ('42', delivery_ledger.RECOVERED_MARKER + text, '102', '9')
        assert adapter.replies == expected_replies
        assert rows('SELECT DISTINCT session_id FROM messages') == [(HOME_SESSION_ID,)]
        assert rows("SELECT role FROM messages WHERE role IN ('user','assistant') ORDER BY id") == [
            ('user',), ('assistant',)] * 2
        transport_key = adapter._event_session_key(first)
        expected = [(delivery_ledger.compute_obligation_id(transport_key, mid, text), 'delivered', mid)
                    for mid in ('101', '102')]
        assert rows('SELECT obligation_id, state, reply_to FROM delivery_obligations ORDER BY created_at') == expected
        # The second Telegram update can be polled again after the gateway reconnects.
        await adapter.handle_message(replace(second))
        if adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(adapter._background_tasks)), timeout=10)
        assert await runner._redeliver_pending_obligations() == 0
        assert len(adapter.replies) == len(provider.calls) == 2
    finally:
        provider.release.set()
        tasks = set(adapter._background_tasks) | {live.task for live in authority.sessions.values() if live.task}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        authority.db.close()
        await asyncio.to_thread(provider.shutdown)
        provider.server_close()
        thread.join(timeout=5)
