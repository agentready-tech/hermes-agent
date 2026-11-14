"""A fresh BotMode home can deliver child results without a prior Telegram message."""
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def control_home(tmp_path, monkeypatch):
    from experiments.shared_runtime.transport import install
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.standalone.panel_chat import MainChat

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '9001')
    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setattr(hermes, '_hosted_homes', frozenset())
    hermes.activate(home)
    ensure_home_session()
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setattr(GatewayRunner, '_instantiate_adapter', GatewayRunner._instantiate_adapter)
    install()
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions'))
    adapter = runner._instantiate_adapter(Platform.TELEGRAM, PlatformConfig(enabled=True, token='fixture'))
    runner.adapters[Platform.TELEGRAM] = adapter
    source = adapter.build_source(chat_id='fixture-chat', user_id='9001', chat_type='dm')
    # This profile has a registered delivery route but no inbound Telegram message.
    entry = runner.session_store.get_or_create_session(source)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='before')
    runner._wire_adapter_handlers(adapter)
    await adapter.connect()
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {
        'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
        'instance_id': 'before', 'native_bootstrap': True})

    class Client:
        async def rpc(self, method, **params):
            response = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            assert 'error' not in response, response
            return response['result']

    chat = MainChat(Client())
    await chat.snapshot()
    await chat.dispatch({'method': 'prompt.submit', 'params': {
        'session_id': 'agentready_home', 'submission_id': 'panel-first', 'text': 'delegate this task'}})
    try:
        yield SimpleNamespace(authority=authority, runner=runner, adapter=adapter, source=source, entry=entry,
                              home=home, chat=chat)
    finally:
        runner.session_store.close_all_db_handles()


def completion(owner):
    from gateway.platforms.event import MessageEvent
    return MessageEvent(text='A child finished', source=owner.source, internal=True, metadata={
        'gateway_session_key': owner.entry.session_key, 'gateway_session_id': owner.entry.session_id})


@pytest.mark.asyncio
async def test_control_only_home_admits_and_recovers_its_own_completion(control_home):
    import json
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    owner = control_home
    rows = list_session_admissions(owner.authority.db, session_id='agentready_home', pending_only=False)
    assert len(rows) == 1 and 'native_text_v1' not in rows[0]['payload']  # no fabricated human ingress
    await owner.chat.dispatch({'method': 'prompt.submit', 'params': {
        'session_id': 'agentready_home', 'submission_id': 'panel-first', 'text': 'delegate this task'}})
    assert 'native_origin_v1' not in json.dumps(await owner.chat.snapshot())
    authority = await initialize_session_authority(owner.runner, profile_id=str(owner.home), instance_id='after')
    assert await authority.recover_native_sessions([
        ('agentready_home', owner.source, owner.adapter)]) == {'agentready_home': 'ready'}
    await authority.admit_automation(owner.adapter, completion(owner), 'own-child')
    await authority.admit_automation(owner.adapter, completion(owner), 'own-child')
    rows = list_session_admissions(authority.db, session_id='agentready_home', pending_only=False)
    assert [row['request_id'] for row in rows] == ['panel-first', 'own-child']


@pytest.mark.asyncio
async def test_control_origin_does_not_bind_a_foreign_authority(control_home):
    from gateway.session_contract import Principal, SessionRef, Submission
    from hermes_state_runtime import RuntimeStoreError
    owner = control_home
    owner.authority.profile_id = str(owner.home / 'foreign')
    actor = Principal('fixture-panel', owner.authority.profile_id, frozenset({'session:submit'}), 'viewer')
    with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
        await owner.authority.submit(actor, Submission(request_id='foreign',
            ref=SessionRef(owner.authority.profile_id, 'agentready_home'), intent='queue',
            payload={'text': 'must not bind the other authority'}))


@pytest.mark.asyncio
async def test_control_origin_cannot_follow_a_rotated_connector(control_home):
    from hermes_state_runtime import RuntimeStoreError
    owner = control_home
    owner.adapter.config.token = 'different-fixture'
    with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
        await owner.authority.admit_automation(owner.adapter, completion(owner), 'wrong-connector')


@pytest.mark.asyncio
async def test_control_client_cannot_supply_a_route_proof(control_home):
    from gateway.session_contract import Submission, SessionRef
    from gateway.session_contract import Principal
    from hermes_state_runtime import RuntimeStoreError
    owner = control_home
    actor = Principal('fixture-panel', str(owner.home), frozenset({'session:submit'}), 'viewer')
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        await owner.authority.submit(actor, Submission(request_id='forged',
            ref=SessionRef(str(owner.home), 'agentready_home'), intent='queue',
            payload={'text': 'forged', 'native_origin_v1': {}}))
