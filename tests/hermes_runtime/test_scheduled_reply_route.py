"""A managed timer must retain its gateway reply route instead of waking only the UI."""

from datetime import datetime
import json
import sqlite3
import time

import pytest


@pytest.fixture
def telegram_home(monkeypatch):
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY

    home = get_hermes_home()
    home.mkdir(exist_ok=True)
    (home / 'config.yaml').write_text('terminal:\n  backend: docker\n')
    monkeypatch.setattr(hermes, '_home', None)
    activate_home(home)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='owner', chat_type='dm', thread_id='77')
    entry = SessionEntry(HOME_SESSION_KEY, HOME_SESSION_ID, datetime.now(), datetime.now(), origin=source,
                         platform=Platform.TELEGRAM)
    with sqlite3.connect(home / 'state.db') as conn:
        conn.execute('INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES(?,?,?,?)',
                     (str((home / 'sessions').resolve()), HOME_SESSION_KEY, json.dumps(entry.to_dict()), time.time()))
    from gateway.session_context import set_session_vars, clear_session_vars
    tokens = set_session_vars(platform='telegram', chat_id='42', chat_type='dm', thread_id='77',
                              user_id='owner', session_key=HOME_SESSION_KEY, session_id=HOME_SESSION_ID)
    try:
        yield home, entry
    finally:
        clear_session_vars(tokens)


def test_timer_uses_native_gateway_delivery_with_same_telegram_reply_route(telegram_home):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.sessions import HOME_SESSION_KEY
    from hermes_runtime.standalone.events import admit_cron, schedule

    home, _ = telegram_home
    job = schedule('Return the scheduled result', 'every 1h', 'Telegram follow-up')
    first = admit_cron(job, execution_id='one-fire')
    assert first[0] is True
    with sqlite3.connect(home / 'state.db') as conn:
        events = [json.loads(row[0]) for row in conn.execute('SELECT event_json FROM async_delegations')]
    assert len(events) == 1
    event = events[0]
    assert event['session_key'] == HOME_SESSION_KEY
    assert event['platform'] == 'telegram'
    assert event['chat_id'] == '42'
    assert event['thread_id'] == '77'
    assert 'Return the scheduled result' in event['summary']
    assert not HomeInbox().pending()
    from types import SimpleNamespace
    from tui_gateway import server
    assert not server._session_owns_notification_event('internal-viewer', {
        'agent':SimpleNamespace(session_id='agentready_home'), 'session_key':'agentready_home'}, event)
    runner = GatewayRunner(GatewayConfig())
    try:
        source = runner._build_process_event_source(event)
        assert source.platform == Platform.TELEGRAM
        assert source.chat_id == '42' and source.thread_id == '77'
        assert admit_cron(job, execution_id='one-fire') == first
        with sqlite3.connect(home / 'state.db') as conn:
            assert conn.execute('SELECT count(*) FROM async_delegations').fetchone()[0] == 1
    finally:
        from hermes_logging import _reset_queued_handlers
        _reset_queued_handlers()


def test_unrelated_gateway_route_cannot_receive_main_schedule(telegram_home):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron, schedule

    home, entry = telegram_home
    entry.session_id = 'unrelated-conversation'
    with sqlite3.connect(home / 'state.db') as conn:
        conn.execute('UPDATE gateway_routing SET entry_json=?', (json.dumps(entry.to_dict()),))
    admit_cron(schedule('Keep this in main', 'every 1h'), execution_id='wrong-route')
    assert len(HomeInbox().pending()) == 1
    with sqlite3.connect(home / 'state.db') as conn:
        assert conn.execute('SELECT count(*) FROM async_delegations').fetchone()[0] == 0


def test_web_timer_does_not_switch_to_registered_telegram_chat(telegram_home):
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron, schedule

    home, _ = telegram_home
    tokens = set_session_vars(platform='tui', session_id='agentready_home')
    try:
        job = schedule('Return this in the web conversation', 'every 1h')
    finally:
        clear_session_vars(tokens)
    assert job['origin']['reply_to'] == {'kind':'main'}
    admit_cron(job, execution_id='web-fire')
    assert len(HomeInbox().pending()) == 1
    with sqlite3.connect(home / 'state.db') as conn:
        assert conn.execute('SELECT count(*) FROM async_delegations').fetchone()[0] == 0
