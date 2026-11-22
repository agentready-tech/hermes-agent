"""AGE-6 managed monitor admission with a source that cannot touch the network."""

from threading import Event

from tests.hermes_runtime.test_main_schedule_tools import _tool

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def test_managed_monitor_baseline_quiet_change_failure_and_cancel(main_home, monkeypatch):
    from cron import monitor
    from cron.jobs import get_job, list_jobs
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
    ok, doc, reply, err = fire('equal')
    assert ok and 'no_change' in doc and reply == SILENT_MARKER and err is None
    assert inbox.pending() == [(baseline_id, baseline)]
    assert get_job(job_id)['monitor_state'] == stored_baseline
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
