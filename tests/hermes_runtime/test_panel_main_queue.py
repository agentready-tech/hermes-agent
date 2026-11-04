"""A busy BotMode accepts Panel input in its owner's existing FIFO, without a second writer."""
import asyncio
import json
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.fixture
def main_chat(main_home, monkeypatch):
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    from hermes_runtime.standalone.panel_chat import MainChat

    home, db = main_home
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='fixture-dm', user_id='fixture-person')
    entry = SessionEntry(HOME_SESSION_KEY, HOME_SESSION_ID, datetime.now(), datetime.now(), origin=source)
    store = SimpleNamespace(_lock=threading.RLock(), _ensure_loaded_locked=lambda: None,
                            _entries={HOME_SESSION_KEY: entry})
    runner = SimpleNamespace(_draining=False, session_store=store, config=SimpleNamespace(multiplex_profiles=False))
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='fixture', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='fixture'))
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: None), {
        'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
        'instance_id': 'fixture', 'native_bootstrap': True})

    class Client:
        def __init__(self):
            self.sent = []
            self.websocket = SimpleNamespace(send=self.send)

        async def send(self, frame):
            self.sent.append(json.loads(frame))

        async def rpc(self, method, **params):
            from hermes_cli.gateway_client import rpc_error
            response = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            if 'error' in response:
                raise rpc_error(response['error'])
            return response['result']

    client = Client()
    yield MainChat(client), authority, db, client


@pytest.mark.asyncio
@pytest.mark.parametrize('started', [False, True])
async def test_busy_main_queues_once_and_preserves_single_history(main_chat, started):
    from hermes_state_runtime import claim_session_input, list_session_admissions
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    from tui_gateway.server import _canonical_ledger_refusal
    chat, authority, db, _client = main_chat
    before = db.get_messages(HOME_SESSION_ID)
    await chat.snapshot()
    await chat.dispatch({'method': 'prompt.submit', 'params': {'session_id': HOME_SESSION_ID,
        'submission_id': 'older-work', 'text': 'A preceding synthetic task'}})
    if started:
        claim_session_input(db, epoch=authority.epoch, session_id=HOME_SESSION_ID)
    # The old direct writer really refuses this same ledger, which must remain protected.
    assert _canonical_ledger_refusal({'session_key': HOME_SESSION_ID}) is not None
    request = {'method': 'prompt.submit', 'params': {'session_id': HOME_SESSION_ID,
        'submission_id': 'stable-panel-input', 'text': 'Continue the same conversation'}}
    first = await chat.dispatch(request)
    retry = await chat.dispatch(request)
    assert first['admission_id'] == retry['admission_id']
    assert first['status'] == 'queued'
    admissions = list_session_admissions(db, session_id=HOME_SESSION_ID)
    assert [r['request_id'] for r in admissions] == ['older-work', 'stable-panel-input']
    assert all(r['target_session_id'] == HOME_SESSION_ID for r in admissions)
    assert db.get_messages(HOME_SESSION_ID) == before  # accepting does not execute a model
    with db._read_ctx() as conn:
        assert [r[0] for r in conn.execute('SELECT id FROM sessions')] == [HOME_SESSION_ID]
    assert authority.sessions[HOME_SESSION_ID].route == HOME_SESSION_KEY


@pytest.mark.asyncio
async def test_secrets_use_native_response_frames_not_submissions(main_chat):
    chat, _authority, _db, client = main_chat
    await chat.snapshot()
    answer = {'jsonrpc': '2.0', 'id': 'srq-fixture-code', 'result': {'value': 'synthetic-not-a-secret'}}
    await chat.answer(answer)
    assert client.sent == [answer]
    with pytest.raises(ValueError, match='Unknown native request'):
        await chat.answer({'id': 'unowned', 'result': {'value': 'synthetic'}})


@pytest.mark.asyncio
async def test_main_only_and_required_retry_identity(main_chat):
    chat, _authority, _db, _client = main_chat
    with pytest.raises(ValueError, match='main conversation'):
        await chat.dispatch({'method': 'session.resume', 'params': {'session_id': 'other'}})
    await chat.snapshot()
    with pytest.raises(ValueError, match='stable submission'):
        await chat.dispatch({'method': 'prompt.submit', 'params': {'session_id': 'agentready_home', 'text': 'fixture'}})


def test_secret_reconnect_replays_description_without_answer():
    from gateway import session_sudo_prompt as prompts
    pending = prompts._Pending('agentready_home')
    pending.request = {'id': 'srq-fixture', 'method': 'vault.code', 'params': {'site': 'fixture'}}
    pending.value = 'must-never-be-projected'
    with prompts._lock:
        prompts._open['srq-fixture'] = pending
    try:
        snapshot = prompts.open_requests('agentready_home')
        assert snapshot == [pending.request]
        assert 'must-never-be-projected' not in json.dumps(snapshot)
        assert prompts.open_requests('another-conversation') == []
    finally:
        with prompts._lock:
            prompts._open.pop('srq-fixture', None)


def test_managed_panel_turn_keeps_masked_native_vault_forms(main_home):
    from gateway.config import Platform
    from gateway.session_cli_turn import cli_turn_scope
    from gateway.session_events import SessionEvents
    from gateway.session_sudo_prompt import resolve
    from gateway.session_vault_prompt import vault_prompt_scope
    from agent.vault_backends import unlock
    from tests.waiting import wait_until
    events, frames = SessionEvents(), []
    peer = SimpleNamespace(write=lambda frame: frames.append(frame) or True)
    events.fanout.attach(peer)
    authority = SimpleNamespace(sessions={'agentready_home': SimpleNamespace(event_stream=events)})
    turn = SimpleNamespace(_approval_owner=(authority, 'agentready_home', 1), _ctx=SimpleNamespace(
        source=SimpleNamespace(platform=Platform.TELEGRAM), inbound_message_id='panel-admission'))
    previous = unlock.get_code_prompt_callback()
    out = {}
    with cli_turn_scope('panel-admission', {'clarify_batch': True}), vault_prompt_scope(turn, SimpleNamespace()):
        callback = unlock.get_code_prompt_callback()
        worker = threading.Thread(target=lambda: out.setdefault('code', callback('fixture-site', 'fixture-hint')))
        worker.start()
        try:
            wait_until(lambda: bool(frames), timeout=3, what='masked native code form')
            request = frames[0]
            assert request['method'] == 'vault.code'
            assert request['params']['site'] == 'fixture-site'
            assert resolve({'id': request['id'], 'result': {'value': 'synthetic-code'}})
        finally:
            worker.join(3)
    assert out == {'code': 'synthetic-code'}
    assert 'synthetic-code' not in json.dumps(frames)
    assert unlock.get_code_prompt_callback() is previous
