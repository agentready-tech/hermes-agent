"""Exact external-wait bindings on ordinary prompt-only cron records."""

from contextlib import closing, contextmanager
from datetime import UTC, datetime
import json
import re
import sqlite3
from uuid import uuid4

BINDING_PAUSE = 'External wait binding pending'


def new_wait_timer_id():
    return 'wait-' + uuid4().hex


def is_wait_timer(job):
    # Even if a store loses metadata, this ID may never act as a generic job.
    job_id = job.get('id')
    return 'external_wait' in job or (isinstance(job_id, str) and re.fullmatch(r'wait-[a-f0-9]{32}', job_id) is not None)


def binding(project_id, wait_id, state='bound'):
    return {'project_id': project_id, 'wait_id': wait_id, 'state': state}


def store_home():
    from cron.jobs import _current_cron_store
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters.hermes import managed_home

    home = get_hermes_home().resolve()
    store = _current_cron_store()
    directory = home / 'cron'
    if (managed_home() != home or store.cron_dir != directory or directory.resolve() != directory
            or store.jobs_file != directory / 'jobs.json' or store.jobs_file.is_symlink()
            or (directory / '.jobs.lock').is_symlink() or (home / 'state.db').is_symlink()):
        raise ValueError('External waits require the owning profile cron store')
    return home


def records():
    from cron.jobs import _peek_jobs_unlocked

    jobs = _peek_jobs_unlocked()
    if jobs is None or any(not isinstance(job, dict) for job in jobs):
        raise ValueError('External wait timer store cannot be verified')
    return jobs


def exact_job(jobs, job_id, project_id, wait_id):
    matches = [job for job in jobs if job.get('id') == job_id or
               any(job.get('external_wait') == binding(project_id, wait_id, state) for state in ('staged', 'bound'))]
    if not matches:
        return None
    if (len(matches) != 1 or matches[0].get('id') != job_id or matches[0].get('external_wait') not in
            (binding(project_id, wait_id), binding(project_id, wait_id, 'staged'))):
        raise ValueError('Conflicting external wait timer identity')
    return matches[0]


def validate_timer(job):
    from hermes_runtime.standalone.events import main_schedule_origin, validate_main_schedule

    validate_main_schedule(job)
    prompt, schedule, repeat = (job.get(key) for key in ('prompt', 'schedule', 'repeat'))
    if (job.get('origin') != main_schedule_origin() or job.get('deliver') != 'local'
            or not isinstance(prompt, str) or not prompt.strip()
            or not isinstance(schedule, dict) or schedule.get('kind') not in ('cron', 'interval')
            or not isinstance(repeat, dict) or repeat.get('times') is not None):
        raise ValueError('External waits require an unbounded recurring main prompt with a wait deadline')


def stage_job(job, identity):
    """Publish only a fresh ID with its tag and PAUSED state in the same write."""
    from cron.jobs import _jobs_lock, save_jobs

    job_id, project_id, wait_id = identity
    validate_timer(job)
    if job.get('enabled') is not False or job.get('state') != 'paused':
        raise ValueError('External wait timer must be staged paused')
    store_home()
    with _jobs_lock(strict=True):
        jobs = records()
        existing = exact_job(jobs, job_id, project_id, wait_id)
        if existing is not None:
            return existing
        job['external_wait'] = binding(project_id, wait_id, 'staged')
        save_jobs(jobs + [job])
    return job


def _active_wait(home, project_id, wait_id, job_id):
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_state_runtime import _canonical_chain

    with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('SELECT workspace_json, parent_session_id FROM delegated_projects '
                           'WHERE subagent_id=? AND archived=0 AND paused=0', (project_id,)).fetchone()
        if row is None or row[1] not in _canonical_chain(conn, HOME_SESSION_ID):
            return False
        wait = json.loads(row[0]).get('external_wait', {})
        if not isinstance(wait, dict) or wait.get('state') != 'BOUND':
            return False
        deadline = datetime.fromisoformat(wait['deadline'])
        return ((wait.get('job_id'), wait.get('wait_id'), wait.get('mode'), wait.get('return_path'))
                == (job_id, wait_id, 'resume_main', 'timer')
                and deadline.tzinfo is not None and datetime.now(UTC) < deadline)


@contextmanager
def admitted_binding(job):
    """Check the current bound pair under the same per-job fence as removal."""
    from cron.jobs import _fire_job_lock, _jobs_lock, is_job_runnable, is_terminal_job

    tag = job.get('external_wait')
    job_id = job.get('id')
    if (not isinstance(job_id, str) or re.fullmatch(r'wait-[a-f0-9]{32}', job_id) is None
            or not isinstance(tag, dict) or tag.get('state') != 'bound'):
        raise ValueError('External wait timer is not bound')
    project_id, wait_id = tag.get('project_id'), tag.get('wait_id')
    if not isinstance(project_id, str) or not isinstance(wait_id, str):
        raise ValueError('Invalid external wait timer identity')
    home = store_home()
    with _fire_job_lock(job_id) as acquired:
        if not acquired:
            raise ValueError('External wait fire fence unavailable')
        with _jobs_lock(strict=True):
            current = exact_job(records(), job_id, project_id, wait_id)
            if (current is None or current.get('external_wait') != tag or not is_job_runnable(current)
                    or is_terminal_job(current)):
                raise ValueError('External wait timer is not runnable')
            validate_timer(current)
        if not _active_wait(home, project_id, wait_id, job_id):
            raise ValueError('External wait timer does not match an active project wait')
        yield tag, current
