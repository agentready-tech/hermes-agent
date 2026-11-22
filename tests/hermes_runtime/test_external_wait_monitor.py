"""AGE-6 managed monitor admission with a source that cannot touch the network."""

from datetime import datetime, timedelta
from threading import Event

from tests.hermes_runtime.test_main_schedule_tools import _tool

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def test_managed_monitor_baseline_quiet_change_failure_and_cancel(main_home, monkeypatch):
    from cron import monitor
    from cron.jobs import advance_next_run, get_job, list_jobs
    from cron.scheduler import SILENT_MARKER, run_job
    from hermes_runtime.adapters.inbox import HomeInbox

    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    result = _tool('create', schedule='every 1h', prompt='Review the fixture observation')
    assert result['success'], result
    job_id = result['job_id']
    reads = []
    samples = iter([(True, 'initial'), (True, 'initial'), (True, 'changed'), (False, 'fixture unavailable')])

    def source(job):
        reads.append(job['id'])
        return next(samples)

    monkeypatch.setattr(monitor, 'job_has_monitor', lambda job: job['id'] == job_id)
    monkeypatch.setattr(monitor, '_run_monitor_source', source)

    def fire(occurrence, **kwargs):
        return run_job(get_job(job_id), execution_id=occurrence, **kwargs)

    inbox = HomeInbox()
    assert fire('baseline')[0]
    [(baseline_id, baseline)] = inbox.pending()
    assert 'Monitor Baseline' in baseline
    assert 'untrusted_monitor_observation' in baseline
    stored_baseline = get_job(job_id)['monitor_state']
    old_slot = datetime.fromisoformat(get_job(job_id)['next_run_at'])
    now = old_slot + timedelta(seconds=1)
    monkeypatch.setattr('cron.jobs._hermes_now', lambda: now)
    assert advance_next_run(job_id)
    ok, doc, reply, err = fire('equal')
    assert ok and 'no_change' in doc and reply == SILENT_MARKER and err is None
    assert inbox.pending() == [(baseline_id, baseline)]
    assert get_job(job_id)['monitor_state'] == stored_baseline
    assert datetime.fromisoformat(get_job(job_id)['next_run_at']) > now
    assert fire('change', extra_prompt='manual fixture instructions')[0]
    assert len(inbox.pending()) == 2
    changed = next(text for identity, text in inbox.pending() if identity != baseline_id)
    assert 'MONITOR CHANGE DETECTED' in changed
    assert 'manual fixture instructions' in changed.split('[UNTRUSTED EVENT DATA]', 1)[1]
    stored_change = get_job(job_id)['monitor_state']
    assert stored_change != stored_baseline
    assert fire('error')[0] is False
    cancelled = Event()
    cancelled.set()
    assert fire('cancelled', cancel_event=cancelled)[3] == 'cancelled'
    assert reads == [job_id] * 4
    assert len(inbox.pending()) == 2
    assert get_job(job_id)['monitor_state'] == stored_change
    assert [job['id'] for job in list_jobs()] == [job_id]


def test_equal_monitor_output_is_silent_through_scheduler_tick(main_home, monkeypatch):
    from unittest.mock import Mock

    from cron import jobs, monitor, scheduler, scheduler_tick
    from cron.executions import latest_execution
    from hermes_runtime.adapters.inbox import HomeInbox

    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    result = _tool('create', schedule='every 1h', prompt='Review offline fixture')
    assert result['success'], result
    job_id = result['job_id']
    monkeypatch.setattr(monitor, 'job_has_monitor', lambda job: job['id'] == job_id)
    read_source = Mock(return_value=(True, 'unchanged fixture'))
    monkeypatch.setattr(monitor, '_run_monitor_source', read_source)
    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', Mock(return_value=False))
    monkeypatch.setattr(scheduler_tick, '_pre_dispatch_housekeeping', Mock())
    for name in ('_maybe_run_worktree_maintenance', '_sweep_mcp_orphans', '_sweep_stale_inflight_for_tick'):
        monkeypatch.setattr(scheduler, name, Mock())
    monkeypatch.setattr(scheduler, '_should_yield_tick_to_fresh_gateway', Mock(return_value=None))
    model = Mock(side_effect=AssertionError('A quiet tick must not create an agent'))
    monkeypatch.setattr('run_agent.AIAgent', model)

    job = jobs.get_job(job_id)
    assert scheduler.run_job(job, execution_id='fixture-baseline')[0]
    hash_before = jobs.get_job(job_id)['monitor_state']
    inbox_before = HomeInbox().pending()
    due = datetime.fromisoformat(jobs.get_job(job_id)['next_run_at'])
    monkeypatch.setattr(jobs, '_hermes_now', lambda: due)
    monkeypatch.setattr(scheduler, '_hermes_now', lambda: due)
    assert scheduler.tick(verbose=False) == 1

    receipt = latest_execution(job_id)
    assert receipt['status'] == 'completed'
    assert receipt['delivery_outcome'] == 'suppressed'
    current = jobs.get_job(job_id)
    assert current['last_status'] == 'no_change'
    assert datetime.fromisoformat(current['next_run_at']) > due
    assert current['monitor_state'] == hash_before
    assert [record['id'] for record in jobs.list_jobs()] == [job_id]
    assert HomeInbox().pending() == inbox_before
    assert read_source.call_count == 2
    model.assert_not_called()
