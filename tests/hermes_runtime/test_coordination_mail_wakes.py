"""Mailbox wakes use the native FIFO without becoming user-facing replies."""

import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from queue import Queue
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from tests.hermes_runtime.test_busy_telegram_replies import _Telegram
from tests.hermes_runtime.test_standalone_service_e2e import Provider
from tests.waiting import async_wait_until


def test_crash_recovery_does_not_recreate_a_hidden_coordination_reply():
    from gateway.run_startup import GatewayStartupMixin

    visible_prompt = {'role': 'user', 'content': 'A different prompt', 'timestamp': 10}
    hidden_reply = {'role': 'assistant', 'content': 'A private coordination reply', 'timestamp': 11,
                    'display_kind': 'hidden', 'display_metadata': {'notification_category': 'diagnostic'}}
    origin = SimpleNamespace(platform=Platform.TELEGRAM)
    assert GatewayStartupMixin()._crash_left_reply([visible_prompt, hidden_reply], 10, origin) == ''
    assert GatewayStartupMixin()._crash_left_reply([visible_prompt, {'role': 'assistant', 'timestamp': 11,
                                                                   'content': 'A real reply'}], 10, origin) == 'A real reply'


@pytest.mark.asyncio
async def test_mailbox_wakes_and_real_project_results_have_distinct_native_delivery(tmp_path, monkeypatch):
    from gateway import delivery_ledger
    from gateway.run import GatewayRunner
    from gateway.session_automation import mailbox_wake_disposition
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionRef
    from hermes_runtime.adapters import hermes
    from tools.delegate_mail import publish_pending_mail
    from tools.delegate_projects import _publish_completed
    from tools.process_registry import process_registry

    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setattr(process_registry, 'completion_queue', Queue())
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
    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='coordination-wakes')
    adapter = _Telegram()
    adapter.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='9')
    first = MessageEvent(text='Delivery Friday', source=source, message_id='101')
    origin = {**source.to_dict(), 'session_key': adapter._event_session_key(first), 'wake': True}
    watcher = None

    def query(sql, parameters=()):
        with closing(sqlite3.connect(tmp_path / 'state.db')) as db, db:
            return db.execute(sql, parameters).fetchall()

    def enqueue_mail(mail_id, target, *, root=None):
        query('''INSERT INTO delegation_mail(message_id,root_session_id,sender_session_id,target_session_id,
                  content,content_hash,kind,origin_json,accepted_at) VALUES(?,?,?,?,?,?,'message',?,?)''',
              (mail_id, root or parent, 'sender', target, 'Fixture coordination detail', 'fixture', json.dumps(origin), time.time()))

    def complete(execution_id, result):
        store = SimpleNamespace(parent_id=parent)
        project = {'project_key': 'fixture', 'subagent_id': 'child-fixture', 'goal': 'A delegated fixture'}
        message = {'message_id': execution_id, 'origin_json': json.dumps(origin), 'accepted_at': time.time()}
        _publish_completed(store, project, message, {'status': 'completed', 'summary': result})

    def settled(count):
        return query("SELECT count(*) FROM session_admissions WHERE status='terminal'") == [(count,)]

    def replay_event(identity):
        [(raw,)] = query('SELECT event_json FROM async_delegations WHERE delegation_id=?', (identity,))
        return json.loads(raw)

    def native_payload(*ids):
        identities = [json.dumps(['async_delegation', value, '']) for value in ids]
        return {'native_text_v1': {'automation': {'identity': identities[0], 'owner': parent,
                                                  'identities': identities}}}

    try:
        await adapter.handle_message(first)
        await async_wait_until(provider.entered.is_set, timeout=20, what='first model call')
        [(parent,)] = query('SELECT target_session_id FROM session_admissions WHERE request_id=?', ('101',))
        obsolete = 'agent-mail-' + 'a' * 32
        enqueue_mail(obsolete, 'child-fixture')
        assert publish_pending_mail() == 1
        complete('project-result-1', 'First verified project result.')
        runner._running = True
        watcher = asyncio.create_task(runner._async_delegation_watcher(interval=0.01))
        await async_wait_until(lambda: query('SELECT count(*) FROM session_admissions') == [(3,)],
                               timeout=15, what='separate admissions for the mixed drain')
        query('UPDATE delegation_mail SET delivered_at=? WHERE message_id=?', (time.time(), obsolete))
        provider.release.set()
        await async_wait_until(lambda: settled(3) and len(adapter.replies) == 2, timeout=20,
                               what='human and first result sent, stale wake settled')
        assert len(provider.calls) == 2
        assert any('First verified project result.' in json.dumps(call) for call in provider.calls)

        pending = 'agent-mail-' + 'b' * 32
        enqueue_mail(pending, 'child-fixture')
        assert publish_pending_mail() == 1
        await async_wait_until(lambda: settled(4), timeout=20, what='pending child coordination ran silently')
        assert len(provider.calls) == 3
        assert len(adapter.replies) == 2
        assert query("SELECT count(*) FROM messages WHERE role='assistant' AND display_kind='hidden'") == [(1,)]

        complete('project-result-2', 'New execution of the same project.')
        await async_wait_until(lambda: settled(5) and len(adapter.replies) == 3, timeout=20,
                               what='second project execution remains visible')
        assert any('New execution of the same project.' in json.dumps(call) for call in provider.calls)
        for identity in (obsolete, pending, 'project-result-1', 'project-result-2'):
            process_registry.completion_queue.put(replay_event(identity))
        await async_wait_until(process_registry.completion_queue.empty, timeout=5, what='replayed completions inspected')
        runner._running = False
        await asyncio.wait_for(watcher, timeout=5)
        assert settled(5)
        assert len(provider.calls) == 4
        assert len(adapter.replies) == 3
        assert query("SELECT state,count(*) FROM delivery_obligations GROUP BY state") == [('delivered', 3)]
        assert await runner._redeliver_pending_obligations() == 0

        unread = 'agent-mail-' + 'c' * 32
        unrelated = 'agent-mail-' + 'd' * 32
        enqueue_mail(unread, parent)
        enqueue_mail(unrelated, 'other-child', root='other-owner')
        ref = SessionRef(authority.profile_id, parent)
        assert mailbox_wake_disposition(authority, ref, native_payload(unread)) is None
        assert mailbox_wake_disposition(authority, ref, native_payload(unrelated)) is None
        assert mailbox_wake_disposition(authority, ref, native_payload(pending, 'project-result-2')) is None
    finally:
        provider.release.set()
        runner._running = False
        tasks = set(adapter._background_tasks) | {live.task for live in authority.sessions.values() if live.task}
        if watcher is not None:
            tasks.add(watcher)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        authority.db.close()
        await asyncio.to_thread(provider.shutdown)
        provider.server_close()
        thread.join(timeout=5)
