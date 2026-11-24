"""Native main owns the lifecycle of its ordinary external-wait timer."""

from datetime import UTC, datetime
import json
import sqlite3


def create(agent, args):
    from cron.jobs import compute_next_run, parse_schedule
    from cron.wait_binding import new_wait_timer_id, store_home
    from hermes_runtime.adapters.goal_turn import goal_in_scope
    from tools.cronjob_tools import _scan_cron_prompt
    from tools.delegate_external_wait import _existing, _text, wait_turn
    from tools.delegate_project_store import ProjectStore
    from tools.registry import tool_error

    project, wait_id = args.get('project_id'), args.get('wait_id')
    turn = wait_turn(agent)
    mode = 'resume_child' if getattr(turn, 'parent_goal_id', None) else 'resume_main'
    fields = {'action', 'project_id', 'wait_id', 'prompt', 'schedule', 'name'}
    if (args.get('action') != 'create' or args.keys() - fields or not _text(project, 128)
            or not _text(wait_id, 128) or getattr(turn, 'external_wait', None) != (project, wait_id)):
        return tool_error('Select the exact open wait in its owning conversation before creating its timer')
    prompt, schedule, name = args.get('prompt'), args.get('schedule'), args.get('name')
    if (not isinstance(prompt, str) or not prompt.strip() or not isinstance(schedule, str)
            or (name is not None and not isinstance(name, str))):
        return tool_error('An external wait timer requires a prompt and a recurring schedule')
    error = _scan_cron_prompt(prompt)
    if error:
        return tool_error(error)
    definition = {'prompt': prompt, 'schedule': schedule, 'name': name}

    def reserve(workspace, _conn):
        wait = _existing(workspace, wait_id)
        if (wait['state'] not in ('OPEN', 'RESERVED', 'BOUND') or wait.get('mode') != mode
                or wait.get('return_path') != 'timer' or datetime.fromisoformat(wait['deadline']) <= datetime.now(UTC)):
            raise ValueError('The wait cannot reserve a timer')
        if wait['state'] == 'OPEN':
            if next_run is None or not now < next_run < datetime.fromisoformat(wait['deadline']):
                raise ValueError('The timer must have a future slot before the wait deadline')
            wait.update(state='RESERVED', job_id=new_wait_timer_id(), timer_definition=definition)
        elif wait.get('timer_definition') != definition:
            raise ValueError('The exact wait already has a different timer reservation')
        return wait['job_id']

    try:
        if not goal_in_scope(agent, project):
            raise ValueError('Project Goal is outside the scope of this wake')
        store_home()
        parsed = parse_schedule(schedule)
        if parsed['kind'] not in ('cron', 'interval'):
            raise ValueError('An external wait timer must recur')
        now = datetime.now(UTC)
        slot = compute_next_run(parsed)
        next_run = datetime.fromisoformat(slot) if slot else None
        with ProjectStore(agent) as store:
            job_id = store.mutate_workspace(project, reserve)
            job = _bind_and_resume(store, project, wait_id, job_id, definition)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        return tool_error(str(exc))
    from cron.scheduler import _notify_provider_jobs_changed

    _notify_provider_jobs_changed()
    return json.dumps({'success': True, 'job_id': job_id, 'next_run_at': job['next_run_at'],
                       'execution_scope': 'child' if mode == 'resume_child' else 'main'})


def _bind_and_resume(store, project, wait_id, job_id, definition):
    from cron.jobs import _fire_job_lock, _jobs_lock, create_job, is_job_runnable, resume_job, save_jobs
    from cron.wait_binding import BINDING_PAUSE, binding, exact_job, records, validate_timer
    from hermes_runtime.standalone.events import main_schedule_origin
    from tools.delegate_external_wait import _existing

    def check(wait):
        if wait['job_id'] != job_id or wait['state'] not in ('RESERVED', 'BOUND'):
            raise ValueError('External wait timer reservation no longer active')
        if datetime.fromisoformat(wait['deadline']) <= datetime.now(UTC):
            raise ValueError('External wait deadline passed')
        return wait

    def mark_bound(workspace, _conn):
        check(_existing(workspace, wait_id))['state'] = 'BOUND'

    with _fire_job_lock(job_id) as acquired:
        if not acquired:
            raise ValueError('External wait fire fence unavailable')
        wait = check(_existing(store.read_workspace(project), wait_id))
        with _jobs_lock(strict=True):
            jobs = records()
            job = exact_job(jobs, job_id, project, wait_id)
            if job is None:
                if wait['state'] != 'RESERVED':
                    raise ValueError('Bound timer missing; do not create a replacement')
                job = create_job(**definition, origin=main_schedule_origin(), deliver='local', paused=True,
                                 paused_reason=BINDING_PAUSE,
                                 _wait_timer=(job_id, project, wait_id))
                jobs = records()
                job = exact_job(jobs, job_id, project, wait_id)
            validate_timer(job)
            if job['external_wait']['state'] == 'staged':
                if job.get('enabled') is not False or job.get('state') != 'paused':
                    raise ValueError('Staged timer unexpectedly activated')
                job['external_wait'] = binding(project, wait_id)
                save_jobs(jobs)
        store.mutate_workspace(project, mark_bound)
        with _jobs_lock(strict=True):
            job = exact_job(records(), job_id, project, wait_id)
            if job is None or job.get('external_wait') != binding(project, wait_id):
                raise ValueError('Bound timer changed before resume')
            if is_job_runnable(job):
                return job
            if job.get('paused_reason') != BINDING_PAUSE:
                raise ValueError('The bound timer was paused outside its pending bind; it was not resumed')
            resumed = resume_job(job_id)
            if resumed is None:
                raise ValueError('External wait timer resume not confirmed')
            return resumed


def close(store, project, wait_id, args):
    from tools.delegate_external_wait import validate_close

    snapshot = json.loads(store.get(project)['workspace_json']).get('external_wait')
    if not isinstance(snapshot, dict) or snapshot.get('wait_id') != wait_id:
        raise ValueError('No current wait with this exact ID')

    def authorize(current, conn):
        return validate_close(current, conn, store, args)

    return settle((project, wait_id, snapshot['job_id']), _mutator(store, project), authorize)


def _mutator(store, project):
    from functools import partial

    return partial(store.mutate_workspace, project, active_only=False)


def archive_project(store, project, paused):
    from tools.delegate_project_store import set_project_paused

    project_id = project['subagent_id']
    mutate = _mutator(store, project_id)
    snapshot = json.loads(project['workspace_json']).get('external_wait')

    def archive(current, conn):
        set_project_paused(conn, project_id, paused, True)
        if current.get('state') in ('CLOSING', 'CLOSED'):
            return current['close_reason']
        return 'closed'

    if isinstance(snapshot, dict):
        return settle((project_id, snapshot['wait_id'], snapshot['job_id']), mutate, archive)

    def archive_without_wait(workspace, conn):
        if workspace.get('external_wait') is not None:
            raise ValueError('External wait changed before archive; retry the project archive')
        archive({}, conn)

    return mutate(archive_without_wait)


def settle(identity, mutate, authorize):
    """Share the fenced CLOSING -> exact removal -> CLOSED path for terminal owners."""
    from cron.jobs import _fire_job_lock, _jobs_lock, remove_job
    from cron.wait_binding import exact_job, is_wait_timer_id, matches_wait, records, store_home

    project, wait_id, job_id = identity
    if not isinstance(wait_id, str) or not wait_id or (job_id is not None and not is_wait_timer_id(job_id)):
        raise ValueError('Invalid external wait cleanup identity')

    def exact_wait(workspace):
        current = workspace.get('external_wait')
        if not matches_wait(current, wait_id, job_id):
            raise ValueError('External wait changed before closure; retry the exact operation')
        return current

    def begin(workspace, conn):
        current = exact_wait(workspace)
        reason = authorize(current, conn)
        if current['state'] in ('CLOSING', 'CLOSED'):
            if current.get('close_reason') != reason:
                raise ValueError('External wait already has a different terminal outcome')
        elif current['state'] in ('OPEN', 'RESERVED', 'BOUND'):
            current.update(state='CLOSING' if job_id is not None else 'CLOSED', close_reason=reason)
        else:
            raise ValueError('External wait state cannot be closed')
        return current

    def finish(workspace, _conn):
        current = exact_wait(workspace)
        if current['state'] not in ('CLOSING', 'CLOSED'):
            raise ValueError('External wait cleanup state changed')
        current['state'] = 'CLOSED'
        return current

    if job_id is None:
        return mutate(begin)
    store_home()
    with _fire_job_lock(job_id) as acquired:
        if not acquired:
            raise ValueError('External wait fire fence unavailable')
        mutate(begin)
        with _jobs_lock(strict=True):
            if exact_job(records(), job_id, project, wait_id) is not None:
                remove_job(job_id)
            if exact_job(records(), job_id, project, wait_id) is not None:
                raise ValueError('External wait timer removal not confirmed')
        closed = mutate(finish)
    from cron.scheduler import _notify_provider_jobs_changed

    _notify_provider_jobs_changed()
    return closed
