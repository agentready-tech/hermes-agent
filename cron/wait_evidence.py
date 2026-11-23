"""Repair-free evidence for a selected managed external wait."""

from datetime import datetime


def live_timer(wait, project_id, now, *, until_deadline=False):
    from cron.jobs import _peek_jobs_unlocked
    from cron.wait_binding import exact_job
    from hermes_runtime.standalone.events import main_schedule_origin, validate_main_schedule

    job_id = wait.get('job_id')
    if wait.get('state') != 'BOUND' or not isinstance(job_id, str) or not job_id:
        return None
    jobs = _peek_jobs_unlocked()
    if jobs is None or any(not isinstance(job, dict) for job in jobs):
        return None
    try:
        job = exact_job(jobs, job_id, project_id, wait['wait_id'])
    except (ValueError, KeyError):
        return None
    binding = {'project_id': project_id, 'wait_id': wait['wait_id'], 'state': 'bound'}
    if job is None or job.get('external_wait') != binding:
        return None
    if (job.get('origin') != main_schedule_origin() or job.get('enabled') is not True
            or job.get('paused_at') or job.get('state') not in ('scheduled', 'running')):
        return None
    schedule, repeat = job.get('schedule'), job.get('repeat')
    if not isinstance(schedule, dict) or schedule.get('kind') not in ('cron', 'interval'):
        return None
    if not isinstance(repeat, dict) or type(repeat.get('completed')) is not int or repeat['completed'] < 0:
        return None
    times = repeat.get('times')
    if times is not None and (type(times) is not int or times <= repeat['completed']):
        return None
    try:
        validate_main_schedule(job)
        next_run = datetime.fromisoformat(job.get('next_run_at'))
        deadline = datetime.fromisoformat(wait.get('deadline'))
        if next_run.tzinfo is None or deadline.tzinfo is None:
            return None
        # An established wait belongs to cron until its deadline, including
        # between claiming the last usable slot and admitting that occurrence.
        active = now < deadline if until_deadline else now < next_run < deadline
        return job if active else None
    except (ValueError, TypeError):
        return None


def has_live_timer(wait, project_id, now, *, until_deadline=False):
    return live_timer(wait, project_id, now, until_deadline=until_deadline) is not None
