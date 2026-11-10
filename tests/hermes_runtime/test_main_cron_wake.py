"""A persisted main timer wake must enter its owning native FIFO without a viewer."""
import json
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


async def owner(home, db, monkeypatch, instance):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    config = GatewayConfig(multiplex_profiles=False)
    config.sessions_dir = home / 'sessions'
    runner = GatewayRunner(config)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id=instance, db=db)
    monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
    return runner, authority


@pytest.mark.asyncio
async def test_pending_cron_wake_recovers_once_into_main_fifo_without_viewer(main_home, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from hermes_runtime.standalone.events import admit_cron, main_schedule_origin
    from hermes_state_runtime import RuntimeStoreError, list_session_admissions
    home, db = main_home
    before = db.get_messages('agentready_home')
    _first, first = await owner(home, db, monkeypatch, 'before-restart')
    connection = AuthorityConnection(first, SimpleNamespace(write=lambda _frame: True), {
        'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
        'instance_id': 'before-restart', 'native_bootstrap': True})
    mounted = await connection.dispatch({'id': 1, 'method': 'session.resume',
        'params': {'session_id': 'agentready_home', 'display_projection': True}})
    assert 'error' not in mounted, mounted.get('error')
    queued = await connection.dispatch({'id': 2, 'method': 'prompt.submit', 'params': {
        'session_id': 'agentready_home', 'submission_id': 'before-timer', 'text': 'Synthetic earlier input'}})
    assert 'error' not in queued, queued.get('error')
    job = {'id': 'fixture-main-timer', 'origin': main_schedule_origin(), 'deliver': 'local',
           'prompt': 'Synthetic scheduled input'}
    assert admit_cron(job, execution_id='fixture-fire')[0]
    with db._read_ctx() as conn:
        row = conn.execute("SELECT event_json,delivery_state FROM async_delegations WHERE delegation_id LIKE 'main-cron-%'").fetchone()
    event = json.loads(row['event_json'])
    assert event['platform'] == 'agentready' and row['delivery_state'] == 'pending'

    runner, restored = await owner(home, db, monkeypatch, 'after-restart')
    assert not restored.sessions['agentready_home'].subscribers
    refusals = []
    original = restored.admit_automation

    async def observed(*args):
        try:
            return await original(*args)
        except RuntimeStoreError as error:
            refusals.append(error.reason)
            raise

    monkeypatch.setattr(restored, 'admit_automation', observed)
    result = await runner._deliver_completion_notification('Synthetic scheduled input', event)
    assert result is True, refusals
    rows = list_session_admissions(db, session_id='agentready_home', pending_only=False)
    assert len(rows) == 2 and rows[0]['request_id'] == 'before-timer'
    assert rows[1]['principal_id'] == 'automation:agent:main:agentready:home'
    assert rows[1]['status'] == 'queued'
    assert await runner._deliver_completion_notification('Synthetic scheduled input', event) is None
    assert len(list_session_admissions(db, session_id='agentready_home', pending_only=False)) == 2
    with db._read_ctx() as conn:
        assert conn.execute('SELECT delivery_state FROM async_delegations WHERE delegation_id=?',
                            (event['delegation_id'],)).fetchone()[0] == 'delivered'
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1
    assert db.get_messages('agentready_home') == before

    # The accepted input survives another owner restart and is revalidated before claim.
    third_runner, third = await owner(home, db, monkeypatch, 'after-admission-restart')
    live = third.sessions['agentready_home']
    source = live.source
    assert await third.recover_native_sessions([
        ('agentready_home', source, third_runner._adapter_for_source(source))]) == {'agentready_home': 'ready'}
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import cancel_session_input
    cancel_session_input(db, epoch=third.epoch, admission_id=rows[0]['admission_id'])
    claimed, _head = await third._claim_next(SessionRef(str(home), 'agentready_home'), live)
    assert claimed['admission_id'] == rows[1]['admission_id']
    from gateway.session_automation import restore_local_automation
    restored_event = restore_local_automation(third, SessionRef(str(home), 'agentready_home'), claimed)
    assert restored_event.internal and restored_event.source.platform.value == 'agentready'
    assert db.get_messages('agentready_home') == before


@pytest.mark.asyncio
@pytest.mark.parametrize('forgery', ['profile', 'adapter-owner', 'current-route'])
async def test_main_automation_rejects_foreign_or_stale_delivery_before_commit(main_home, monkeypatch, forgery):
    from dataclasses import replace
    from gateway.platforms.event import MessageEvent
    from hermes_runtime.adapters.main_transport import MainAdapter
    from hermes_state_runtime import RuntimeStoreError, list_session_admissions
    home, db = main_home
    runner, authority = await owner(home, db, monkeypatch, 'guard-fixture')
    live = authority.sessions['agentready_home']
    source = live.source
    adapter = runner._adapter_for_source(source)
    if forgery == 'profile':
        source = replace(source, profile='foreign-profile')
    elif forgery == 'adapter-owner':
        adapter = MainAdapter(SimpleNamespace(sessions=authority.sessions))
    else:
        entry = runner.session_store.lookup_by_session_key(live.route)
        entry.origin = replace(source, user_id='different-owner')
    event = MessageEvent(text='Synthetic producer input', source=source, internal=True,
        metadata={'gateway_session_key': live.route, 'gateway_session_id': 'agentready_home'})
    with pytest.raises(RuntimeStoreError):
        await authority.admit_automation(adapter, event, 'fixture-producer')
    assert list_session_admissions(db, session_id='agentready_home', pending_only=False) == []
