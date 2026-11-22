"""Repair-free evidence for a selected managed external wait."""

from datetime import datetime


def has_live_timer(wait, project_id, now):
    from cron.jobs import _peek_jobs_unlocked
    from hermes_runtime.standalone.events import main_schedule_origin, validate_main_schedule

    job_id = wait.get('job_id')
    if wait.get('state') != 'BOUND' or not isinstance(job_id, str) or not job_id:
        return False
    jobs = _peek_jobs_unlocked()
    if jobs is None or any(not isinstance(job, dict) for job in jobs):
        return False
    matches = [job for job in jobs if job.get('id') == job_id]
    if len(matches) != 1:
        return False
    job = matches[0]
    binding = {'project_id': project_id, 'wait_id': wait['wait_id'], 'state': 'bound'}
    if job.get('external_wait') != binding or sum(item.get('external_wait') == binding for item in jobs) != 1:
        return False
    if (job.get('origin') != main_schedule_origin() or job.get('enabled') is not True
            or job.get('paused_at') or job.get('state') not in ('scheduled', 'running')):
        return False
    schedule, repeat = job.get('schedule'), job.get('repeat')
    if not isinstance(schedule, dict) or schedule.get('kind') not in ('cron', 'interval'):
        return False
    if not isinstance(repeat, dict) or type(repeat.get('completed')) is not int or repeat['completed'] < 0:
        return False
    times = repeat.get('times')
    if times is not None and (type(times) is not int or times <= repeat['completed']):
        return False
    try:
        validate_main_schedule(job)
        next_run = datetime.fromisoformat(job.get('next_run_at'))
        deadline = datetime.fromisoformat(wait.get('deadline'))
        return next_run.tzinfo is not None and deadline.tzinfo is not None and now < next_run < deadline
    except (ValueError, TypeError):
        return False
