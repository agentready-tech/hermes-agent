"""Timer evidence is exact and uses raw, repair-free cron reads."""

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.parametrize('change', [None, 'foreign', 'paused', 'past', 'naive', 'exhausted', 'corrupt'])
def test_wait_evidence_from_current_store(change):
    from cron.jobs import _current_cron_store, create_job, update_job
    from cron.wait_evidence import has_live_timer
    from hermes_runtime.standalone.events import main_schedule_origin

    job = create_job(prompt='Review the fixture', schedule='every 1h', origin=main_schedule_origin())
    now = datetime.now(UTC)
    next_run = now + timedelta(minutes=5)
    wait = {'state': 'BOUND', 'job_id': job['id'], 'wait_id': 'wait-a',
            'deadline': (now + timedelta(hours=1)).isoformat()}
    binding = {'project_id': 'project-a', 'wait_id': 'wait-a', 'state': 'bound'}
    updates = {'external_wait': binding, 'next_run_at': next_run.isoformat()}
    changes = {'foreign': {'external_wait': {**binding, 'project_id': 'project-b'}},
               'paused': {'paused_at': now.isoformat()}, 'past': {'next_run_at': now.isoformat()},
               'naive': {'next_run_at': next_run.replace(tzinfo=None).isoformat()},
               'exhausted': {'repeat': {'times': 1, 'completed': 1}}}
    update_job(job['id'], {**updates, **changes.get(change, {})})
    path = _current_cron_store().jobs_file
    if change == 'corrupt':
        path.write_bytes(b'{broken')
    before = path.read_bytes()
    assert has_live_timer(wait, 'project-a', now) is (change is None)
    assert path.read_bytes() == before
