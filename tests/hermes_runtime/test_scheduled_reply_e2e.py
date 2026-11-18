"""A native gateway publishes main-session answers through the committed route."""

import asyncio
from datetime import datetime
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
import time

import pytest

from tests.hermes_runtime.test_standalone_service_e2e import Provider
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("main_route", ["telegram", "agentready"])
@pytest.mark.parametrize("producer", ["timer", "timer-autonomous", "timer-autonomous-busy", "mail", "mail-busy", "panel-queue"])
async def test_main_event_replies_through_committed_route_and_shared_history(tmp_path, monkeypatch, producer, main_route):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry
    from gateway.session_authority import initialize_session_authority
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    import tools.async_delegation
    import agent.prompt_builder

    class Adapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token='owned-fixture'), Platform.TELEGRAM)
            self.deliveries = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            pass

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.deliveries.append({'chat_id':chat_id, 'content':content, 'metadata':metadata})
            return SendResult(success=True, message_id='fixture-final')

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None):
            pass

        async def get_chat_info(self, chat_id):
            return {'id':chat_id}

    home = tmp_path / 'state'
    home.mkdir()
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    provider.calls = []
    expected = 'Delivery reviewed in the same main session.'
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{provider.server_address[1]}/v1'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OPENAI_API_KEY', 'loopback-only')
    monkeypatch.setenv('OPENAI_BASE_URL', url)
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setattr(tools.async_delegation, 'ORPHAN_SWEEP_INTERVAL_S', 0.05)
    monkeypatch.setattr(agent.prompt_builder, '_probe_remote_backend', lambda *_: 'Linux Docker fixture')
    (home / '.env').write_text('OPENAI_API_KEY=loopback-only\nTELEGRAM_ALLOWED_USERS=fixture-user\n')
    (home / 'config.yaml').write_text(json.dumps({
        'gateway':{'multiplex_profiles':False},
        'model':{'provider':'custom', 'default':'gpt-4.1', 'base_url':url},
        'terminal':{'backend':'docker','cwd':str(home)}, 'compression':{'enabled':False},
        'streaming':{'enabled':False}, 'approvals':{'mode':'off'},
        'auxiliary':{'title_generation':{'enabled':False}}}))
    activate_home(home)
    runner = GatewayRunner()
    adapter = Adapter()
    adapter.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = adapter
    source = adapter.build_source(chat_id='schedule-chat', chat_type='dm', user_id='fixture-user')
    if main_route == 'telegram':
        # A previously bound main route survives owner startup. Sending an ordinary
        # Telegram message only selects its own reply transport.
        now = datetime.now()
        entry = SessionEntry(HOME_SESSION_KEY, HOME_SESSION_ID, now, now, origin=source, platform=source.platform)
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES(?,?,?,?)',
                       (str(runner.config.sessions_dir.resolve()), HOME_SESSION_KEY, json.dumps(entry.to_dict()), time.time()))
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='scheduled-reply-fixture')
    runner._wire_adapter_handlers(adapter)
    watcher = None
    try:
        from hermes_runtime.adapters.state import main_gateway_origin
        assert main_gateway_origin()['platform'] == main_route
        await adapter.handle_message(MessageEvent(text='Remember this existing conversation', source=source, message_id='warm'))
        await async_wait_until(lambda: any(expected in x['content'] for x in adapter.deliveries),
                               timeout=25, what='initial transport reply')
        initial_deliveries = len(adapter.deliveries)
        stream = authority.sessions[HOME_SESSION_ID].event_stream
        epoch, cursor = stream.watermark()
        with sqlite3.connect(home / 'state.db') as db:
            before = db.execute('SELECT id FROM sessions ORDER BY id').fetchall()
        assert before == [(HOME_SESSION_ID,)]
        if producer == 'panel-queue':
            from types import SimpleNamespace
            from gateway.session_controls import AuthorityConnection
            from hermes_runtime.standalone.panel_chat import MainChat
            connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: None), {
                'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
                'instance_id': authority.instance_id, 'native_bootstrap': True})

            class Client:
                async def rpc(self, method, **params):
                    response = await connection.dispatch({'id': 1, 'method': method, 'params': params})
                    assert 'error' not in response, response
                    return response['result']

            chat = MainChat(Client())
            await chat.snapshot()
            scheduled = authority._schedule
            monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
            request = {'method': 'prompt.submit', 'params': {'session_id': HOME_SESSION_ID,
                'submission_id': 'synthetic-panel-nonce', 'text': 'PANEL_MAIN_ROUTE_PROOF'}}
            one = await chat.dispatch(request)
            two = await chat.dispatch(request)
            assert one['admission_id'] == two['admission_id']
            monkeypatch.setattr(authority, '_schedule', scheduled)
            from gateway.session_contract import SessionRef
            scheduled(SessionRef(str(home), HOME_SESSION_ID))
        elif producer.startswith('timer'):
            import tools.cronjob_tools
            from tools.registry import registry
            from cron import scheduler
            from cron.jobs import get_job
            tokens = set_session_vars(platform='' if 'autonomous' in producer else 'telegram',
                                      chat_id='' if 'autonomous' in producer else 'schedule-chat', chat_type='dm',
                                      user_id='fixture-user', session_key=HOME_SESSION_KEY, session_id=HOME_SESSION_ID)
            try:
                result = json.loads(registry.dispatch('cronjob_manage', {'action':'create', 'schedule':'every 1h',
                    'prompt':'SCHEDULED_MAIN_ROUTE_PROOF: continue this conversation and return the result.'},
                    session_id=HOME_SESSION_ID))
            finally:
                clear_session_vars(tokens)
            assert result['success'], result
            monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lambda *_: None)
            from hermes_runtime.adapters.turns import ActiveHomeTurn
            turn = ActiveHomeTurn() if producer == 'timer-autonomous-busy' else None
            try:
                assert await asyncio.to_thread(scheduler.run_one_job, get_job(result['job_id']))
                if turn:
                    assert turn.take() == [], 'A foreground turn cannot capture the scheduled reply transport'
            finally:
                if turn:
                    turn.finish(error='Synthetic foreground turn finished')
        else:
            from hermes_runtime.adapters.turns import ActiveHomeTurn
            from hermes_runtime.standalone.workspace import admit_batches
            response = {'batches': [{'connectionId': 'mail-fixture', 'cursor': {'uid': 1}, 'events': [{
                'id': 'mail-1', 'source': 'gmail:fixture', 'prompt': 'MAIL_MAIN_ROUTE_PROOF', 'metadata': {}}]}]}
            turn = ActiveHomeTurn() if producer == 'mail-busy' else None
            try:
                admit_batches(home, response, {})
                admit_batches(home, response, {})
                if turn:
                    assert turn.take() == [], 'A Panel-owned turn cannot capture the mail reply'
            finally:
                if turn:
                    turn.finish(error='Synthetic foreground turn finished')
        runner._running = True
        watcher = asyncio.create_task(runner._async_delegation_watcher(interval=0.05))

        def shared_reply():
            return [frame for frame in stream.since(epoch, cursor)['events']
                    if frame['type'] == 'message.complete' and frame['payload'].get('text') == expected]

        await async_wait_until(shared_reply, timeout=25, what='reply in the shared main stream')
        if main_route == 'telegram':
            await async_wait_until(lambda: len(adapter.deliveries) > initial_deliveries,
                                   timeout=25, what='proactive Telegram reply')
        proactive = adapter.deliveries[initial_deliveries:]
        assert [(row['chat_id'], row['content']) for row in proactive] == (
            [('schedule-chat', expected)] if main_route == 'telegram' else []
        )
        assert len(shared_reply()) == 1
        marker = 'PANEL' if producer == 'panel-queue' else 'SCHEDULED' if producer.startswith('timer') else 'MAIL'
        assert marker + '_MAIN_ROUTE_PROOF' in json.dumps(provider.calls)
        assert len([call for call in provider.calls if 'messages' in call]) == 2, 'A replay must not start another inference'
        with sqlite3.connect(home / 'state.db') as db:
            assert db.execute('SELECT id FROM sessions ORDER BY id').fetchall() == before
            replies = db.execute("SELECT content FROM messages WHERE role='assistant' ORDER BY id").fetchall()
            assert replies == [(expected,), (expected,)]
            assert db.execute("SELECT count(*) FROM agentready_inbox").fetchone()[0] == 0
            if producer != 'panel-queue':
                assert db.execute("SELECT count(*) FROM session_admissions WHERE principal_id LIKE 'automation:%'").fetchone()[0] >= 1
    finally:
        runner._running = False
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        from gateway.run_runtime import _retire_profile_authority
        await _retire_profile_authority(authority, timeout=5)
        provider.shutdown()
        provider.server_close()
        from hermes_logging import _reset_queued_handlers
        _reset_queued_handlers()
