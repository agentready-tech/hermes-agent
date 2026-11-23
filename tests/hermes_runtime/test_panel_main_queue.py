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
    runner = SimpleNamespace(_draining=False, session_store=store, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
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
async def test_subagent_timeline_loads_through_main_chat(main_chat):
    from hermes_runtime.sessions import HOME_SESSION_ID

    chat, _authority, db, _client = main_chat
    child = 'timeline-child'
    db.create_session(child, source='subagent', parent_session_id=HOME_SESSION_ID)
    db.append_message(child, 'user', 'Check the report')
    db.append_message(child, 'assistant', 'The report is ready', finish_reason='stop')

    await chat.dispatch({'method': 'session.resume', 'params': {'history_native': True}})
    graph = await chat.dispatch({'method': 'panel.subagents.graph', 'params': {'limit': 6}})
    node = next(node for node in graph['nodes'] if node['id'] == child)
    timeline = await chat.dispatch({'method': 'panel.subagents.timeline', 'params': {
        'node_ids': [{'id': node['id'], 'parent_id': node['parent_id']}], 'tools': False,
    }})

    assert [event['text'] for event in timeline['events']] == ['Check the report', 'The report is ready']


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
@pytest.mark.parametrize('diagnostic', [False, True], ids=['user-turn', 'diagnostic-wake'])
async def test_panel_text_reaches_next_model_call_in_active_tool_turn(main_chat, main_home, monkeypatch, diagnostic):
    from unittest.mock import patch

    from agent.notification_presentation import notification_turn
    from hermes_cli.gateway_client import GatewayRPCError
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_state_runtime import list_session_admissions
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from tests.waiting import async_wait_until

    chat, _authority, db, _client = main_chat
    (main_home[0] / 'config.yaml').write_text('terminal:\n  backend: local\ncompression:\n  enabled: false\n')
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    await chat.snapshot()
    clarify = 'Use the corrected test value'
    tool_started, release = threading.Event(), threading.Event()
    requests = []
    with patch('agent.process_bootstrap.OpenAI'):
        agent = AIAgent(api_key='test-only', base_url='http://127.0.0.1:9/v1', provider='custom',
                        model='gpt-4.1', quiet_mode=True, session_db=db, session_id=HOME_SESSION_ID,
                        skip_context_files=True, skip_memory=True, enabled_toolsets=['terminal'])
    agent.compression_enabled, agent.save_trajectories, agent.tool_delay = False, False, 0

    def model(payload, *args, **kwargs):
        requests.append(payload['messages'])
        if len(requests) == 1:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'tool-before-clarification', 'type': 'function',
                 'function': {'name': 'terminal', 'arguments': '{"command":"printf fixture"}'}},
            ]}
            reason = 'tool_calls'
        else:
            message, reason = {'role': 'assistant', 'content': 'I will use the corrected value.'}, 'stop'
        return ChatCompletion(id='fixture', object='chat.completion', created=1, model='gpt-4.1',
            choices=[{'index': 0, 'message': message, 'finish_reason': reason}],
            usage={'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110})

    def execute_tool(name, *_args, **_kwargs):
        assert name == 'terminal'
        tool_started.set()
        assert release.wait(15)
        return '{"output":"fixture tool finished"}'

    monkeypatch.setattr(agent, '_interruptible_api_call', model)
    monkeypatch.setattr(agent, '_interruptible_streaming_api_call', model)
    monkeypatch.setattr('model_tools.handle_function_call', execute_tool)
    def run_turn():
        with notification_turn(agent, muted=diagnostic, session_id=HOME_SESSION_ID):
            return agent.run_conversation('Run the fixture tool', task_id=HOME_SESSION_ID)

    turn = asyncio.create_task(asyncio.to_thread(run_turn))
    request = {'method': 'prompt.submit', 'params': {'session_id': HOME_SESSION_ID,
        'submission_id': 'stable-panel-clarification', 'text': clarify}}
    another_text = 'Also keep the label'
    another = {'method': 'prompt.submit', 'params': {'session_id': HOME_SESSION_ID,
        'submission_id': 'second-panel-clarification', 'text': another_text}}
    try:
        await async_wait_until(tool_started.is_set, timeout=10, what='running terminal tool')
        confirmations = await asyncio.gather(chat.dispatch(request), chat.dispatch(request))
        assert confirmations[0] == confirmations[1]
        assert confirmations[0]['status'] == 'steered'
        second = await chat.dispatch(another)
        assert second['status'] == 'steered'
        assert list_session_admissions(db, session_id=HOME_SESSION_ID) == []
        release.set()
        result = await asyncio.wait_for(asyncio.shield(turn), timeout=10)
        assert result['final_response'] == 'I will use the corrected value.'
        model_input = requests[1]
        indices = [index for index, row in enumerate(model_input)
                   if row['role'] == 'user' and clarify in str(row.get('content'))]
        assert len(indices) == 1
        assert model_input[indices[0] - 1]['tool_call_id'] == 'tool-before-clarification'
        assert another_text in model_input[indices[0]]['content']
        rows = db.get_messages(HOME_SESSION_ID)
        assert next(row for row in rows if row['content'] == 'Run the fixture tool').get('display_kind') == (
            'hidden' if diagnostic else None)
        for text in (clarify, another_text, result['final_response']):
            matching = [row for row in rows if text in str(row['content'])]
            assert len(matching) == 1 and matching[0].get('display_kind') != 'hidden'
        with db._read_ctx() as conn:
            delivered = conn.execute('SELECT message_id FROM agentready_inbox WHERE delivered_at IS NOT NULL').fetchall()
        assert {row[0] for row in delivered} == {confirmations[0]['admission_id'], second['admission_id']}
        assert await chat.dispatch(request) == confirmations[0]
        conflict = {**request, 'params': {**request['params'], 'text': 'Different input with the same nonce'}}
        with pytest.raises(GatewayRPCError, match='admission_conflict'):
            await chat.dispatch(conflict)
        assert list_session_admissions(db, session_id=HOME_SESSION_ID) == []
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(turn), timeout=10)


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


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['panel', 'telegram'])
@pytest.mark.parametrize('reply', ['valid', 'foreign_state', 'foreign_port', 'provider_rejected', 'cancelled'])
async def test_catalog_oauth_uses_the_originating_chats_secret_prompt(main_home, monkeypatch, channel, reply):
    from urllib.parse import urlencode, urlsplit

    from gateway.config import Platform
    from gateway.session_cli_turn import cli_turn_scope
    from gateway.session_events import SessionEvents
    from gateway.session_secret_prompt import secret_prompt_scope
    from gateway.session_sudo_prompt import resolve
    from hermes_cli import mcp_config
    from mcp.shared.auth import OAuthToken
    from tools import mcp_tool_discovery
    from tools.connectors import mcp
    from tools.connectors.tool import manage_connections
    from tools.mcp_dashboard_oauth import get_dashboard_oauth_flow
    from tools.mcp_oauth import HermesTokenStorage, _make_callback_waiter
    from tests.waiting import async_wait_until

    received, registered, prompts, frames = [], [], [], []
    redirect = ''
    code, state = 'synthetic-authorize-code', 'issued-for-this-install'

    async def exchange():
        nonlocal redirect
        flow = get_dashboard_oauth_flow()
        auth_url = 'https://oauth.example.test/authorize?' + urlencode({
            'redirect_uri': flow.redirect_uri, 'state': state,
        })
        callback_uri = flow.redirect_uri
        if reply == 'foreign_port':
            callback_uri = f'http://127.0.0.1:{urlsplit(callback_uri).port + 1}/callback'
        redirect = '' if reply == 'cancelled' else callback_uri + '?' + urlencode({
            'code': code, 'state': 'another-install' if reply == 'foreign_state' else state,
        })
        await flow.publish_authorization_url(auth_url)
        callback = await _make_callback_waiter(0, timeout=5)()
        received.append(callback if isinstance(callback, tuple) else (callback.code, callback.state))
        if reply == 'provider_rejected':
            raise RuntimeError(f'Provider rejected the test code {code}')
        await HermesTokenStorage('linear').set_tokens(OAuthToken(access_token='test-token', token_type='Bearer'))

    def probe(name, config, *, connect_timeout, details):
        assert name == 'linear'
        asyncio.run(exchange())
        details['initialized'] = True
        return [('list_issues', 'List issues')]

    monkeypatch.setattr(mcp_config, '_probe_single_server', probe)
    monkeypatch.setattr(mcp_tool_discovery, 'register_mcp_servers', lambda config: registered.extend(config))
    monkeypatch.setattr(mcp, '_registered_tool_names', lambda _: ['mcp_linear_list_issues'])

    class Adapter:
        async def request_secret(self, source, *, label, site):
            assert source.user_id == 'authorized-user'
            prompts.append(label)
            return redirect

    session_id, admission_id = 'agentready_home', 'oauth-admission'
    events = SessionEvents()
    events.fanout.attach(SimpleNamespace(write=lambda frame: frames.append(frame) or True))
    authority = SimpleNamespace(sessions={session_id: SimpleNamespace(event_stream=events)})
    turn = SimpleNamespace(
        _approval_owner=(authority, session_id, 1),
        _runner=SimpleNamespace(_delivery_adapter_for=lambda _: Adapter()),
        _ctx=SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM, user_id='authorized-user'),
                             inbound_message_id=admission_id, _loop_for_step=asyncio.get_running_loop(),
                             _run_still_current=lambda: True),
    )
    args = {'action': 'install', 'connectors': [{'name': 'linear', 'mcp': True}]}
    with cli_turn_scope(admission_id, {'clarify_batch': channel == 'panel'}), secret_prompt_scope(turn, SimpleNamespace()):
        install = asyncio.create_task(asyncio.to_thread(manage_connections, args, session_id=session_id))
        if channel == 'panel':
            await async_wait_until(lambda: install.done() or frames, timeout=5, what='OAuth secret form')
            request = next((frame for frame in frames if frame.get('method') == 'secret'), None)
            assert request is not None, 'Panel OAuth install must open the protected input'
            prompts.append(request['params']['prompt'])
            resolve({'id': request['id'], 'result': {'value': redirect}})
        result = await asyncio.wait_for(install, 6)

    assert len(prompts) == 1 and 'https://oauth.example.test/authorize?' in prompts[0]
    target = json.loads(result)['targets'][0]
    if reply == 'valid':
        assert target['state'] == 'connected'
        assert target['tools'] == ['mcp_linear_list_issues']
        assert registered == ['linear'] and received == [(code, state)]
        assert HermesTokenStorage('linear').has_cached_tokens()
    else:
        assert target['state'] == 'failed'
        assert registered == []
        assert received == ([(code, state)] if reply == 'provider_rejected' else [])
        assert not HermesTokenStorage('linear').has_cached_tokens()
    assert 'connect_url' not in target
    assert code not in json.dumps([frames, prompts, result])
    assert not (main_home[0] / '.env').exists()
