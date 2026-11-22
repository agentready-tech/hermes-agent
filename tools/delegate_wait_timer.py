"""Native main owns the lifecycle of its ordinary external-wait timer."""

from datetime import UTC, datetime
import json
import sqlite3


def create(agent, args):
    from cron.jobs import compute_next_run, parse_schedule
    from cron.wait_binding import new_wait_timer_id, store_home
    from tools.cronjob_tools import _scan_cron_prompt
    from tools.delegate_external_wait import _existing, _owns_main, _text
    from tools.delegate_project_store import ProjectStore
    from tools.registry import tool_error

    project, wait_id = args.get('project_id'), args.get('wait_id')
    fields = {'action', 'project_id', 'wait_id', 'prompt', 'schedule', 'name'}
    if (args.get('action') != 'create' or args.keys() - fields or not _text(project, 128)
            or not _text(wait_id, 128) or not _owns_main(agent)
            or getattr(getattr(agent, '_agentready_active_turn', None), 'external_wait', None) != (project, wait_id)):
        return tool_error('Select the exact open wait in the owning main before creating its timer')
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
        if (wait['state'] not in ('OPEN', 'RESERVED', 'BOUND') or wait.get('mode') != 'resume_main'
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
    return json.dumps({'success': True, 'job_id': job_id, 'next_run_at': job['next_run_at'], 'execution_scope': 'main'})


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
    from cron.jobs import _fire_job_lock, _jobs_lock, remove_job
    from cron.wait_binding import exact_job, records, store_home
    from tools.delegate_external_wait import _close, _existing

    job_id = _existing(store.read_workspace(project), wait_id)['job_id']

    def begin(workspace, conn):
        if _existing(workspace, wait_id)['job_id'] != job_id:
            raise ValueError('External wait timer changed before closure; retry the exact close')
        return _close(workspace, conn, store, wait_id, args)

    if job_id is None:
        return store.mutate_workspace(project, begin)
    store_home()

    def finish(workspace, _conn):
        current = _existing(workspace, wait_id)
        if current['state'] != 'CLOSING' or current['job_id'] != job_id:
            raise ValueError('External wait cleanup state changed')
        current['state'] = 'CLOSED'
        return current

    with _fire_job_lock(job_id) as acquired:
        if not acquired:
            raise ValueError('External wait fire fence unavailable')
        store.mutate_workspace(project, begin)
        with _jobs_lock(strict=True):
            if exact_job(records(), job_id, project, wait_id) is not None:
                remove_job(job_id)
            if exact_job(records(), job_id, project, wait_id) is not None:
                raise ValueError('External wait timer removal not confirmed')
        closed = store.mutate_workspace(project, finish)
    from cron.scheduler import _notify_provider_jobs_changed

    _notify_provider_jobs_changed()
    return closed
