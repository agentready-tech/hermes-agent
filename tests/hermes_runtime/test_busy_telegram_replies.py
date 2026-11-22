"""Managed Telegram keeps the native FIFO and delivery boundary for each input."""

import asyncio
import json
import threading
from contextlib import closing
from dataclasses import replace
from http.server import ThreadingHTTPServer
import sqlite3
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from tests.hermes_runtime.test_standalone_service_e2e import Provider
from tests.waiting import async_wait_until


class _ToolProvider(Provider):
    def do_POST(self):
        step = len(self.server.calls) - self.server.seed_calls
        if not 0 <= step < 2:
            return super().do_POST()
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(body)
        text = ('Checking the requested delivery.', 'I received the clarification. Continuing the review.')[step]
        command = ('review', 'continue-review')[step]
        message = {'role': 'assistant', 'content': text,
                   'tool_calls': [{'id': f'call_review_{step}', 'type': 'function',
                                   'function': {'name': 'terminal', 'arguments': json.dumps({'command': command})}}]}
        response = {'id': f'tool-boundary-fixture-{step}', 'created': 1, 'model': 'gpt-4.1'}
        self.send_response(200)
        if body.get('stream'):
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            message['tool_calls'][0]['index'] = 0
            for delta, finish in [(message, None), ({}, 'tool_calls')]:
                chunk = {**response, 'object': 'chat.completion.chunk',
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({**response, 'object': 'chat.completion',
                                        'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls'}]}).encode())


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
@pytest.mark.parametrize('followup', [
    'at-tool-boundary', 'after-final-model-call', 'native-queue', 'recover-follower', 'explicit-steer',
    'coordination-panel', 'coordination-telegram',
])
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
    coordination = followup.startswith('coordination-')
    with_tool = followup == 'at-tool-boundary' or coordination
    provider = ThreadingHTTPServer(('127.0.0.1', 0), _ToolProvider if with_tool else Provider)
    provider.calls = []
    provider.seed_calls = int(coordination)
    provider.block_next = not with_tool
    browser_cleanups = []
    if followup == 'after-final-model-call':
        provider.replies_by_call = {1: 'First review complete.', 2: 'Follow-up reviewed.'}
        monkeypatch.setattr('tools.browser_tab_runtime.finish_native_tabs',
                            lambda agent, task, **outcome: browser_cleanups.append((task, len(provider.calls), outcome)))
    provider.entered, provider.release = threading.Event(), threading.Event()
    next_tool_entered, next_tool_release = threading.Event(), threading.Event()
    if with_tool:
        def terminal(name, arguments, *_args, **_kwargs):
            assert name == 'terminal'
            entered, release = ((provider.entered, provider.release) if arguments['command'] == 'review'
                                else (next_tool_entered, next_tool_release))
            entered.set()
            assert release.wait(timeout=15)
            return '{"review":"tool finished"}'
        monkeypatch.setattr('model_tools.handle_function_call', terminal)
    if followup in {'native-queue', 'recover-follower'}:
        # The authority can still be busy while its managed mailbox is closed.
        monkeypatch.setattr('hermes_runtime.adapters.gateway.forward_to_active_turn', lambda *_: None)
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
        'display': {'interim_assistant_messages': followup != 'after-final-model-call'},
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

    def rows(sql, parameters=()):
        with closing(sqlite3.connect(tmp_path / 'state.db')) as db, db:
            return db.execute(sql, parameters).fetchall()

    try:
        await adapter.handle_message(first)
        if coordination:
            await async_wait_until(lambda: len(adapter.replies) == 1 and authority.sessions[HOME_SESSION_ID].task.done(),
                                   timeout=20, what='seed reply and bound Telegram origin')
            adapter.replies.clear()
            mail_id = 'agent-mail-' + 'a' * 32
            rows('''INSERT INTO delegation_mail(message_id,root_session_id,sender_session_id,target_session_id,
                    content,content_hash,kind,origin_json,accepted_at) VALUES(?,?,?,?,?,?,'message',?,?)''',
                 (mail_id, HOME_SESSION_ID, 'sender', 'child-fixture', 'Coordination detail', 'fixture', '{}', time.time()))
            identity = json.dumps(['async_delegation', mail_id, ''])
            route = adapter._event_session_key(first)
            await authority.admit_automation(adapter, MessageEvent(
                text='Private coordination wake', source=source, internal=True,
                metadata={'gateway_session_key': route, 'gateway_session_id': HOME_SESSION_ID},
            ), identity)
        await async_wait_until(provider.entered.is_set, timeout=20, what='running tool or first model call')
        live = authority.sessions[HOME_SESSION_ID]
        if followup == 'coordination-panel':
            from gateway.session_controls import AuthorityConnection
            panel = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: True), {
                'user_id': 'panel-fixture', 'provider': 'local', 'profile_id': str(tmp_path),
                'instance_id': 'busy-replies', 'native_bootstrap': True,
            })
            await panel.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': HOME_SESSION_ID}})
            receipt = await panel.dispatch({'id': 2, 'method': 'panel.prompt.submit', 'params': {
                'session_id': HOME_SESSION_ID, 'text': second_text, 'submission_id': 'panel-coordination-correction',
                'clarify_batch': True,
            }})
            assert receipt['result']['status'] == 'steered'
        else:
            await asyncio.wait_for(adapter.handle_message(second), timeout=5)
        before = rows('SELECT request_id, target_session_id, status FROM session_admissions ORDER BY seq')
        provider.release.set()
        clarification_reply = 'I received the clarification. Continuing the review.'
        if with_tool:
            await async_wait_until(next_tool_entered.is_set, timeout=10, what='tool after clarification')
            if followup != 'coordination-panel':
                await async_wait_until(lambda: any(row[1] == clarification_reply for row in adapter.replies),
                                       timeout=5, what='interim Telegram reply during the tool')
            if coordination:
                frames = live.event_stream.since(live.event_stream.epoch, 0)['events']
                visible = ''.join(frame['payload'].get('text') or '' for frame in frames
                                  if frame['type'] in {'message.interim', 'message.delta'})
                assert clarification_reply in visible
                assert 'Checking the requested delivery.' not in visible
            assert not live.task.done()
            next_tool_release.set()
        expected_calls = (3 if with_tool else 2) + provider.seed_calls
        await async_wait_until(lambda: len(provider.calls) == expected_calls and live.task.done(),
                               timeout=20, what='completed model calls')
        if adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(adapter._background_tasks)), timeout=10)
        text = 'Delivery reviewed in the same main session.'
        if coordination:
            model_input = provider.calls[2]['messages']
            tool_index = next(i for i, row in enumerate(model_input) if row['role'] == 'tool')
            assert model_input[tool_index + 1]['role'] == 'user'
            assert second_text in model_input[tool_index + 1]['content']
            assert rows('SELECT COUNT(*) FROM session_admissions') == [(2,)]
            assert rows('SELECT COUNT(*) FROM agentready_inbox WHERE delivered_at IS NOT NULL') == [(1,)]
            for visible in (second_text, clarification_reply):
                assert rows('SELECT display_kind FROM messages WHERE content LIKE ?', ('%' + visible + '%',)) == [(None,)]
            assert rows('SELECT display_kind FROM messages WHERE role=? ORDER BY id DESC LIMIT 1', ('assistant',)) == [(None,)]
            for hidden in ('Private coordination wake', 'Checking the requested delivery.'):
                assert rows('SELECT display_kind FROM messages WHERE content LIKE ?', ('%' + hidden + '%',)) == [('hidden',)]
            assert [item[1] for item in adapter.replies] == (
                [] if followup == 'coordination-panel' else [clarification_reply, text])
            frames = live.event_stream.since(live.event_stream.epoch, 0)['events']
            assert sum(frame['type'] == 'message.complete' for frame in frames) == 2
            return
        if with_tool:
            assert before == [('101', HOME_SESSION_ID, 'started')]
            followup_messages = provider.calls[1]['messages']
            tool_index = next(i for i, row in enumerate(followup_messages) if row['role'] == 'tool')
            assert 'tool finished' in followup_messages[tool_index]['content']
            assert followup_messages[tool_index + 1]['role'] == 'user'
            assert second_text in followup_messages[tool_index + 1]['content']
            assert [item[1] for item in adapter.replies] == ['Checking the requested delivery.', clarification_reply, text]
            assert rows('SELECT COUNT(*) FROM agentready_inbox WHERE delivered_at IS NOT NULL') == [(1,)]
            frames = live.event_stream.since(live.event_stream.epoch, 0)['events']
            commentary = [frame['payload']['text'] for frame in frames if frame['type'] == 'message.interim']
            assert commentary == ['Checking the requested delivery.', clarification_reply]
            visible_input = [frame['payload'] for frame in frames if frame['type'] == 'agentready.event.received']
            assert len(visible_input) == 1 and second_text in visible_input[0]['text']
            return
        if followup == 'after-final-model-call':
            assert before == [('101', HOME_SESSION_ID, 'started')]
            await async_wait_until(lambda: len(adapter.replies) == 2, timeout=5, what='both completed replies')
            assert [item[1] for item in adapter.replies] == ['First review complete.', 'Follow-up reviewed.']
            assert rows("SELECT COUNT(*) FROM delivery_obligations WHERE state='delivered'") == [(2,)]
            assert browser_cleanups == [(HOME_SESSION_ID, 2, {'completed': True, 'interrupted': False, 'failed': False})]
            return
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
        next_tool_release.set()
        tasks = set(adapter._background_tasks) | {live.task for live in authority.sessions.values() if live.task}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        authority.db.close()
        await asyncio.to_thread(provider.shutdown)
        provider.server_close()
        thread.join(timeout=5)
