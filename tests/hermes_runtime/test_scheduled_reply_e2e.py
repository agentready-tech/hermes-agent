"""A real native gateway sends a scheduled main-session answer through its original transport."""

import asyncio
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading

import pytest

from tests.hermes_runtime.test_standalone_service_e2e import Provider
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_docker_terminal_timer_replies_through_telegram_without_desktop_or_send_cli(tmp_path, monkeypatch):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
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
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{provider.server_address[1]}/v1'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OPENAI_API_KEY', 'loopback-only')
    monkeypatch.setenv('OPENAI_BASE_URL', url)
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
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
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='scheduled-reply-fixture')
    adapter = Adapter()
    adapter.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._wire_adapter_handlers(adapter)
    watcher = None
    try:
        source = adapter.build_source(chat_id='schedule-chat', chat_type='dm', user_id='fixture-user')
        await adapter.handle_message(MessageEvent(text='Remember this existing conversation', source=source, message_id='warm'))
        expected = 'Delivery reviewed in the same main session.'
        await async_wait_until(lambda: any(expected in x['content'] for x in adapter.deliveries),
                               timeout=25, what='initial transport reply')
        with sqlite3.connect(home / 'state.db') as db:
            before = db.execute('SELECT id FROM sessions ORDER BY id').fetchall()
        assert before == [(HOME_SESSION_ID,)]
        import tools.cronjob_tools
        from tools.registry import registry
        from cron import scheduler
        from cron.jobs import get_job
        tokens = set_session_vars(platform='telegram', chat_id='schedule-chat', chat_type='dm',
                                  user_id='fixture-user', session_key=HOME_SESSION_KEY, session_id=HOME_SESSION_ID)
        try:
            result = json.loads(registry.dispatch('cronjob_manage', {'action':'create', 'schedule':'every 1h',
                'prompt':'SCHEDULED_TELEGRAM_ROUTE_PROOF: continue this conversation and return the result.'},
                session_id=HOME_SESSION_ID))
        finally:
            clear_session_vars(tokens)
        assert result['success'], result
        monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lambda *_: None)
        assert await asyncio.to_thread(scheduler.run_one_job, get_job(result['job_id']))
        runner._running = True
        watcher = asyncio.create_task(runner._async_delegation_watcher(interval=0.05))
        try:
            await async_wait_until(lambda: len([x for x in adapter.deliveries if expected in x['content']]) >= 2,
                                   timeout=25, what='scheduled transport reply')
        except AssertionError:
            with sqlite3.connect(home / 'state.db') as db:
                print('DELIVERY_ROWS', db.execute('SELECT origin_session,state,delivery_state,delivery_attempts FROM async_delegations').fetchall())
                print('ADMISSIONS', db.execute('SELECT principal_id,status,outcome FROM session_admissions').fetchall())
            print('WATCHER', watcher.done(), repr(watcher.exception()) if watcher.done() else 'running')
            raise
        assert 'SCHEDULED_TELEGRAM_ROUTE_PROOF' in json.dumps(provider.calls)
        assert adapter.deliveries[-1]['chat_id'] == 'schedule-chat'
        with sqlite3.connect(home / 'state.db') as db:
            assert db.execute('SELECT id FROM sessions ORDER BY id').fetchall() == before
            assert db.execute("SELECT count(*) FROM agentready_inbox WHERE event_key LIKE '%cron%'").fetchone()[0] == 0
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
