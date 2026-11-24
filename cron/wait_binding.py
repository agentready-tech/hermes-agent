"""Exact external-wait bindings on ordinary prompt-only cron records."""

from contextlib import closing, contextmanager
from datetime import UTC, datetime
import json
import logging
import re
import sqlite3
from uuid import uuid4

BINDING_PAUSE = 'External wait binding pending'
logger = logging.getLogger(__name__)


def new_wait_timer_id():
    return 'wait-' + uuid4().hex


def is_wait_timer_id(job_id):
    return isinstance(job_id, str) and re.fullmatch(r'wait-[a-f0-9]{32}', job_id) is not None


def is_wait_timer(job):
    # Even if a store loses metadata, this ID may never act as a generic job.
    return 'external_wait' in job or is_wait_timer_id(job.get('id'))


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


def _owned_project(conn, project_id):
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_state_runtime import _canonical_chain
    from tools.delegate_child_goals import validate_wait_owner

    row = conn.execute('SELECT workspace_json, parent_session_id, archived, paused FROM delegated_projects '
                       'WHERE subagent_id=?', (project_id,)).fetchone()
    if row is None:
        raise ValueError('External wait has no verified owning project')
    workspace = json.loads(row[0])
    if not isinstance(workspace, dict):
        raise ValueError('Invalid external wait project workspace')
    child = validate_wait_owner(conn, project_id, workspace)
    if not child and row[1] not in _canonical_chain(conn, HOME_SESSION_ID):
        raise ValueError('External wait has no verified owning main project')
    wait = workspace.get('external_wait')
    if isinstance(wait, dict) and wait.get('mode') != ('resume_child' if child else 'resume_main'):
        raise ValueError('External wait mode does not match its owner')
    return workspace, row[2], row[3]


def matches_wait(wait, wait_id, job_id):
    return (isinstance(wait, dict) and
            (wait.get('wait_id'), wait.get('job_id'), wait.get('return_path')) == (wait_id, job_id, 'timer')
            and wait.get('mode') in {'resume_main', 'resume_child'})


def _active_workspace(conn, project_id, wait_id, job_id):
    from tools.delegate_project_goal import active_goal
    from tools.delegate_project_store import _goal_input_state

    workspace, archived, paused = _owned_project(conn, project_id)
    wait = workspace.get('external_wait')
    if archived or paused or not matches_wait(wait, wait_id, job_id) or wait.get('state') != 'BOUND':
        return None
    goal = workspace.get('goal_state')
    if goal and (not active_goal(goal) or goal['status'] == 'blocked'):
        return None
    if wait['mode'] == 'resume_child' and _goal_input_state(conn, project_id) != 'open':
        return None
    deadline = datetime.fromisoformat(wait['deadline'])
    return workspace if deadline.tzinfo is not None and datetime.now(UTC) < deadline else None


def update_admitted_wait(tag, job_id, change):
    """Commit a poll checkpoint under the caller's existing fire fence."""
    from hermes_cli.sqlite_util import transaction
    from tools.delegate_project_store import write_workspace

    project_id, wait_id = tag['project_id'], tag['wait_id']
    uri = (store_home() / 'state.db').as_uri() + '?mode=rw'
    with transaction(sqlite3.connect(uri, uri=True), immediate=True) as conn:
        workspace = _active_workspace(conn, project_id, wait_id, job_id)
        if workspace is None:
            raise ValueError('External wait changed during the reply check')
        return write_workspace(conn, project_id, workspace, lambda saved, _conn: change(saved['external_wait']))


def _job_identity(job):
    tag, job_id = job.get('external_wait'), job.get('id')
    if (not is_wait_timer_id(job_id)
            or not isinstance(tag, dict) or tag.get('state') not in ('staged', 'bound')):
        return None
    project_id, wait_id = tag.get('project_id'), tag.get('wait_id')
    if not all(isinstance(value, str) and value for value in (project_id, wait_id)):
        return None
    return project_id, wait_id, job_id


def _terminal_reason(wait, archived, now):
    state = wait.get('state')
    if state in ('CLOSING', 'CLOSED'):
        reason = wait.get('close_reason')
        # An older access_lost label alone is not evidence. A committed archive is independent proof.
        if reason in ('reply', 'cancelled', 'closed', 'expired') or (archived == 1 and reason == 'access_lost'):
            return reason
        return None
    if state not in ('RESERVED', 'BOUND'):
        return None
    if archived == 1:
        return 'closed'
    deadline = datetime.fromisoformat(wait['deadline'])
    if deadline.tzinfo is not None and deadline <= now:
        return 'expired'
    return None


def _close_terminal_wait(db_uri, identity, now):
    from hermes_cli.sqlite_util import transaction
    from tools.delegate_project_store import write_workspace
    from tools.delegate_wait_timer import settle

    project_id, wait_id, job_id = identity
    with closing(sqlite3.connect(db_uri + '?mode=ro', uri=True)) as conn:
        workspace, archived, _paused = _owned_project(conn, project_id)
        wait = workspace.get('external_wait')
        if not matches_wait(wait, wait_id, job_id):
            raise ValueError('External wait snapshot does not match the job')
        reason = _terminal_reason(wait, archived, now)
        if reason is None:
            return False

    def mutate(change):
        with transaction(sqlite3.connect(db_uri + '?mode=rw', uri=True), immediate=True) as conn:
            workspace, archived, _paused = _owned_project(conn, project_id)
            if (not matches_wait(workspace.get('external_wait'), wait_id, job_id)
                    or _terminal_reason(workspace['external_wait'], archived, now) != reason):
                raise ValueError('Terminal external wait evidence changed')
            return write_workspace(conn, project_id, workspace, change)

    settle(identity, mutate, lambda _wait, _conn: reason)
    return True


def close_terminal_waits(due, snapshot, now):
    """Finish terminal bindings from the ordinary due scan, after releasing its jobs lock."""
    candidates = [identity for job in snapshot if (identity := _job_identity(job)) is not None]
    if not candidates:
        return due
    try:
        db_uri = (store_home() / 'state.db').as_uri()
    except (ValueError, RuntimeError, OSError):
        return due  # Admission still requires the owning store.
    excluded = set()
    for identity in candidates:
        try:
            if _close_terminal_wait(db_uri, identity, now):
                excluded.add(identity[2])
        except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
            excluded.add(identity[2])  # Other jobs continue; this ID never wakes on ambiguous evidence.
            logger.debug('External wait closure deferred for %s (%s)', identity[2], type(error).__name__)
    return [job for job in due if job['id'] not in excluded]


def _runnable_bound_job(job, identity):
    """Resolve the exact runnable record while the caller holds the jobs lock."""
    from cron.jobs import is_job_runnable, is_terminal_job

    project_id, wait_id, job_id = identity
    current = exact_job(records(), job_id, project_id, wait_id)
    if (current is None or current.get('external_wait') != job['external_wait']
            or not is_job_runnable(current) or is_terminal_job(current)):
        raise ValueError('External wait timer is not runnable')
    claim = job.get('fire_claim')
    if isinstance(claim, dict) and (owner := claim.get('by')):
        active = current.get('fire_claim')
        if not isinstance(active, dict) or active.get('by') != owner:
            raise ValueError('External wait timer fire owner changed')
    validate_timer(current)
    return current


@contextmanager
def admitted_binding(job, *, publish=False):
    """Fence the bound pair. Hold the jobs lock through local publication only."""
    from contextlib import ExitStack
    from cron.jobs import _fire_job_lock, _jobs_lock

    identity = _job_identity(job)
    if identity is None or job['external_wait']['state'] != 'bound':
        raise ValueError('External wait timer is not bound')
    project_id, wait_id, job_id = identity
    home = store_home()
    with _fire_job_lock(job_id) as acquired, ExitStack() as publication:
        if not acquired:
            raise ValueError('External wait fire fence unavailable')
        publication.enter_context(_jobs_lock(strict=True))
        current = _runnable_bound_job(job, identity)
        if not publish:
            publication.close()
        with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as conn:
            workspace = _active_workspace(conn, project_id, wait_id, job_id)
        if workspace is None:
            raise ValueError('External wait timer does not match an active project wait')
        yield current['external_wait'], current, workspace
