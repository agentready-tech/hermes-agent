"""Panel-only managed history must mount without a Telegram-created route."""
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def runtime(home, db, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    config = GatewayConfig()
    config.sessions_dir = home / 'sessions'
    store = SessionStore(config.sessions_dir, config)
    adapters = {}
    runner = SimpleNamespace(_draining=False, session_store=store, config=config, adapters=adapters,
        _adapters_for_profile=lambda _profile: adapters, _adapter_for_source=lambda source: adapters.get(source.platform))
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='fixture', db=db,
        epoch=begin_runtime_epoch(db, instance_id='fixture'))
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: True), {
        'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
        'instance_id': 'fixture', 'native_bootstrap': True})
    return authority, connection


async def resume(connection):
    return await connection.dispatch({'id': 1, 'method': 'session.resume',
        'params': {'session_id': 'agentready_home', 'display_projection': True}})


@pytest.mark.asyncio
async def test_panel_only_history_mount_submit_and_cold_restore(main_home, monkeypatch):
    from hermes_state_runtime import list_session_admissions
    home, db = main_home
    before = db.get_messages('agentready_home')
    db.end_session('agentready_home', 'ws_orphan_reap')
    authority, connection = runtime(home, db, monkeypatch)
    mounted = await resume(connection)
    assert 'error' not in mounted, mounted.get('error')
    assert mounted['result']['session_id'] == 'agentready_home'
    assert db.get_messages('agentready_home') == before
    submission = {'id': 2, 'method': 'prompt.submit', 'params': {
        'session_id': 'agentready_home', 'submission_id': 'fixture-stable-request', 'text': 'A synthetic request'}}
    accepted = await connection.dispatch(submission)
    assert 'error' not in accepted, accepted.get('error')
    ref = next(iter(authority.sessions))
    from gateway.session_contract import SessionRef
    claimed, _head = await authority._claim_next(SessionRef(str(home), ref), authority.sessions[ref])
    assert claimed['request_id'] == 'fixture-stable-request'
    # Retire this epoch without executing any external request, then restore the exact saved route.
    authority2, connection2 = runtime(home, db, monkeypatch)
    restored = await resume(connection2)
    assert 'error' not in restored, restored.get('error')
    assert db.get_messages('agentready_home') == before
    with db._read_ctx() as conn:
        assert [r[0] for r in conn.execute('SELECT id FROM sessions')] == ['agentready_home']
    assert len(list_session_admissions(db, session_id='agentready_home', pending_only=False)) == 1
    assert authority2.sessions['agentready_home'].route == authority.sessions[ref].route


@pytest.mark.asyncio
async def test_bootstrap_requires_native_owner_and_own_profile(main_home, monkeypatch):
    home, db = main_home
    authority, connection = runtime(home, db, monkeypatch)
    connection.native_owner = False
    assert (await resume(connection))['error']['data']['reason'] == 'permission_denied'
    assert not authority.sessions
    connection.native_owner = True
    from dataclasses import replace
    connection.actor = replace(connection.actor, profile_id='foreign-profile')
    assert (await resume(connection))['error']['data']['reason'] in {'permission_denied', 'profile_mismatch'}
    assert not authority.sessions
    with db._read_ctx() as conn:
        assert conn.execute('SELECT count(*) FROM gateway_routing').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_native_messaging_joins_panel_only_main_with_own_delivery(main_home, monkeypatch):
    from dataclasses import replace
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.session_contract import SessionRef
    from hermes_runtime.adapters.main_transport import authorize_source
    from hermes_state_runtime import list_session_admissions
    home, db = main_home
    authority, connection = runtime(home, db, monkeypatch)
    assert 'error' not in await resume(connection)
    live = authority.sessions['agentready_home']
    runner = authority.runner
    assert authorize_source(runner, live.source) is True
    assert authorize_source(runner, replace(live.source, profile='foreign')) is False
    telegram = SessionSource(platform=Platform.TELEGRAM, chat_id='fixture-dm', user_id='fixture-human')
    adapter = SimpleNamespace()
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._adapter_flag = lambda *_args: False
    runner._adapter_profile_for_source = lambda _source: None
    runner._is_user_authorized_for_source = lambda source, **_kw: (
        source.platform == Platform.TELEGRAM and source.user_id == 'fixture-human') or authorize_source(runner, source)
    event = MessageEvent(text='Native synthetic message', source=telegram, message_id='fixture-native')
    receipt = await authority.admit_native(event)
    assert receipt.ref.session_id == 'agentready_home'
    claimed, _head = await authority._claim_next(SessionRef(str(home), 'agentready_home'), live)
    assert claimed['admission_id'] == receipt.admission_id
    from gateway.session_envelope import restore_native
    restored = restore_native(claimed['payload'], runner)
    assert runner._adapter_for_source(restored.source) is adapter
    assert len(list_session_admissions(db, session_id='agentready_home')) == 1
    assert live.source.platform.value == 'agentready'


@pytest.mark.asyncio
async def test_queued_panel_input_recovers_without_reopening_any_viewer(main_home, monkeypatch):
    from hermes_runtime.adapters.main_transport import restore_owner_main
    home, db = main_home
    _first, connection = runtime(home, db, monkeypatch)
    assert 'error' not in await resume(connection)
    accepted = await connection.dispatch({'id': 2, 'method': 'prompt.submit', 'params': {
        'session_id': 'agentready_home', 'submission_id': 'recover-pending', 'text': 'Synthetic saved work'}})
    assert 'error' not in accepted
    second, _unused_connection = runtime(home, db, monkeypatch)
    scheduled = []
    monkeypatch.setattr(second, '_schedule', lambda ref: scheduled.append(ref.session_id))
    restore_owner_main(second)
    live = second.sessions['agentready_home']
    outcome = await second.recover_native_sessions([
        ('agentready_home', live.source, second.runner._adapter_for_source(live.source))])
    assert outcome == {'agentready_home': 'ready'}
    assert scheduled == ['agentready_home']
    assert not live.subscribers
