"""AGE-6 managed monitor admission with a source that cannot touch the network."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hermes_runtime.test_main_schedule_tools import _tool

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools', 'tests.hermes_runtime.test_scheduled_reply_route']


@pytest.mark.parametrize('route', ['main_home', 'telegram_home'], ids=['inbox', 'gateway'])
def test_managed_monitor_baseline_quiet_change_failure_and_cancel(route, request, monkeypatch):
    from cron import monitor
    from cron.jobs import get_job, list_jobs
    from cron.scheduler import SILENT_MARKER, run_job
    from hermes_runtime.adapters.inbox import HomeInbox

    home, _ = request.getfixturevalue(route)
    monkeypatch.setattr('hermes_runtime.adapters.turns.forward_to_active_turn', Mock(return_value=False))
    model = Mock(side_effect=AssertionError('Managed admission must not start the model'))
    monkeypatch.setattr('run_agent.AIAgent', model)
    result = _tool('create', schedule='every 1h', prompt='Review the fixture observation')
    assert result['success'], result
    job_id = result['job_id']
    source = Mock(side_effect=[(True, 'initial'), (True, 'initial'), (True, 'changed'),
                               (True, 'changed again'), (False, 'fixture unavailable')])
    monkeypatch.setattr(monitor, 'job_has_monitor', lambda job: job['id'] == job_id)
    monkeypatch.setattr(monitor, '_run_monitor_source', source)

    def fire(occurrence, **kwargs):
        return run_job(get_job(job_id), execution_id=occurrence, **kwargs)

    inbox = HomeInbox()

    def admitted():
        if route == 'main_home':
            return dict(inbox.pending())
        assert not inbox.pending()
        with closing(sqlite3.connect(home / 'state.db')) as conn:
            rows = [json.loads(row[0]) for row in conn.execute('SELECT event_json FROM async_delegations')]
        assert all(row['parent_session_id'] == 'agentready_home' and row['platform'] == 'telegram' for row in rows)
        return {row['delegation_id']: row['summary'] for row in rows}

    def envelope(text):
        return json.loads(text.splitlines()[1])

    assert fire('baseline')[0]
    [(baseline_id, baseline)] = admitted().items()
    baseline_event = envelope(baseline)
    assert baseline_event['event_id'] == f'{job_id}:baseline'
    assert 'Monitor Baseline' in baseline_event['metadata']['untrusted_monitor_observation']
    stored_baseline = get_job(job_id)['monitor_state']
    ok, doc, reply, err = fire('equal')
    assert ok and 'no_change' in doc and reply == SILENT_MARKER and err is None
    assert admitted() == {baseline_id: baseline}
    assert get_job(job_id)['monitor_state'] == stored_baseline
    extra = 'manual fixture instructions'
    assert fire('change', extra_prompt=extra)[0]
    accepted = admitted()
    [(changed_id, changed)] = [(i, text) for i, text in accepted.items() if i != baseline_id]
    changed_event = envelope(changed)
    assert changed_event['event_id'] == f'{job_id}:change'
    observation = changed_event['metadata']['untrusted_monitor_observation']
    assert 'MONITOR CHANGE DETECTED' in observation and extra not in observation
    assert extra in changed.split('[UNTRUSTED EVENT DATA]', 1)[1]
    # Exercise durable receipt dedup independently of hash suppression.
    duplicate = fire('change')
    assert duplicate[0] and changed_id in duplicate[2]
    assert admitted() == accepted
    stored_change = get_job(job_id)['monitor_state']
    assert stored_change != stored_baseline
    ok, _doc, _reply, err = fire('error')
    assert not ok and err == 'fixture unavailable'
    cancelled = Event()
    cancelled.set()
    assert fire('cancelled', cancel_event=cancelled)[3] == 'cancelled'
    assert source.call_count == 5
    assert admitted() == accepted
    assert get_job(job_id)['monitor_state'] == stored_change
    assert monitor._read_last_output(job_id) == 'changed again'
    assert [job['id'] for job in list_jobs()] == [job_id]
    model.assert_not_called()


def test_monitor_state_and_receipts_are_scoped_to_owning_home(tmp_path, monkeypatch):
    import hermes_state
    from cron import jobs, monitor
    from cron.scheduler import run_job
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.config import activate_home

    (tmp_path / 'config.yaml').write_text('{}', encoding='utf-8')
    homes = [tmp_path / 'profiles' / 'a', tmp_path / 'profiles' / 'b']
    for home in homes:
        home.mkdir(parents=True)
    # Use the scoped default, not conftest's fixed single-profile DB path.
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setattr(hermes, '_hosted_homes', frozenset())
    hermes.activate_profiles(homes)
    monkeypatch.setattr('hermes_runtime.adapters.turns.forward_to_active_turn', Mock(return_value=False))
    job_id = '123456789abc'
    monkeypatch.setattr(jobs, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=job_id)))
    monkeypatch.setattr(monitor, 'job_has_monitor', lambda job: job['id'] == job_id)
    reads = []

    def source(_job):
        home = get_hermes_home()
        reads.append(home)
        return True, home.name

    monkeypatch.setattr(monitor, '_run_monitor_source', source)
    for home in homes:
        with _profile_cron_scope(home):
            activate_home(home)
            result = _tool('create', name='Shared fixture name', schedule='every 1h', prompt='Review fixture')
            assert result['success'] and result['job_id'] == job_id
    with _profile_cron_scope(homes[0]):
        assert run_job(jobs.get_job(job_id), execution_id='same-occurrence')[0]
    with _profile_cron_scope(homes[1]):
        assert not jobs.get_job(job_id).get('monitor_state')
        assert not HomeInbox().pending()
        assert run_job(jobs.get_job(job_id), execution_id='same-occurrence')[0]
    for home in homes:
        with _profile_cron_scope(home):
            assert monitor._read_last_output(job_id) == home.name
            assert jobs.get_job(job_id)['monitor_state']['last_output_hash'] == monitor.hash_monitor_output(home.name)
            [(identity, _)] = HomeInbox().pending()
            assert HomeInbox().event_receipt('cron', f'{job_id}:same-occurrence') == identity
    assert reads == homes


def test_quiet_and_failed_monitor_outputs_are_recorded_through_scheduler_tick(main_home, monkeypatch):
    from cron import jobs, monitor, scheduler, scheduler_tick
    from cron.executions import latest_execution
    from hermes_runtime.adapters.inbox import HomeInbox

    monkeypatch.setattr('hermes_runtime.adapters.turns.forward_to_active_turn', Mock(return_value=False))
    result = _tool('create', schedule='every 1h', prompt='Review offline fixture')
    assert result['success'], result
    job_id = result['job_id']
    monkeypatch.setattr(monitor, 'job_has_monitor', lambda job: job['id'] == job_id)
    read_source = Mock(side_effect=[(True, 'unchanged fixture'), (True, 'unchanged fixture'),
                                   (False, 'fixture unavailable')])
    monkeypatch.setattr(monitor, '_run_monitor_source', read_source)
    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', Mock(return_value=False))
    monkeypatch.setattr(scheduler_tick, '_pre_dispatch_housekeeping', Mock())
    for name in ('_maybe_run_worktree_maintenance', '_sweep_mcp_orphans', '_sweep_stale_inflight_for_tick'):
        monkeypatch.setattr(scheduler, name, Mock())
    monkeypatch.setattr(scheduler, '_should_yield_tick_to_fresh_gateway', Mock(return_value=None))
    model = Mock(side_effect=AssertionError('A quiet or failed tick must not create an agent'))
    monkeypatch.setattr('run_agent.AIAgent', model)

    job = jobs.get_job(job_id)
    assert scheduler.run_job(job, execution_id='fixture-baseline')[0]
    hash_before = jobs.get_job(job_id)['monitor_state']
    inbox_before = HomeInbox().pending()

    def tick_due():
        due = datetime.fromisoformat(jobs.get_job(job_id)['next_run_at'])
        monkeypatch.setattr(jobs, '_hermes_now', lambda: due)
        monkeypatch.setattr(scheduler, '_hermes_now', lambda: due)
        assert scheduler.tick(verbose=False) == 1
        assert datetime.fromisoformat(jobs.get_job(job_id)['next_run_at']) > due
        return latest_execution(job_id)

    receipt = tick_due()
    assert receipt['status'] == 'completed'
    assert receipt['delivery_outcome'] == 'suppressed'
    current = jobs.get_job(job_id)
    assert current['last_status'] == 'no_change'
    assert current['monitor_state'] == hash_before
    failed = tick_due()
    assert failed['id'] != receipt['id']
    assert failed['status'] == 'failed' and failed['error'] == 'fixture unavailable'
    assert jobs.get_job(job_id)['monitor_state'] == hash_before
    assert [record['id'] for record in jobs.list_jobs()] == [job_id]
    assert HomeInbox().pending() == inbox_before
    assert read_source.call_count == 3
    model.assert_not_called()
