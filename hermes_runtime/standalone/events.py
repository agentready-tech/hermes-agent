"""Mail and native schedule admissions share the existing durable main inbox."""

import asyncio
import hashlib
import logging
import uuid

logger = logging.getLogger(__name__)
CRON_ORIGIN = "hermes-autonomous"


def is_main_schedule(job):
    return (job.get("origin") or {}).get("platform") == CRON_ORIGIN


def main_scheduling_enabled():
    """Include detached cron workers, which boot without activating the managed adapter."""
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.standalone.config import settings
    from hermes_constants import get_hermes_home

    return enabled() or settings(get_hermes_home()).get("enabled") is True


def validate_main_schedule(job):
    if (any(job.get(k) for k in ("script", "monitor_script", "monitor_url", "context_from", "skills", "skill",
                                "model", "provider", "base_url", "workdir", "reasoning_effort", "interpreter",
                                "no_agent", "continuity", "attach_to_session"))
            or job.get("enabled_toolsets") is not None):
        raise ValueError("Main-inbox schedules accept prompts; put tool/script work and preferences in the prompt. "
                         "Scheduled work must continue the existing main session.")
    if job.get("deliver") not in (None, "", "local", "origin", "bot-chat") or job.get("failure_deliver"):
        raise ValueError("Main-inbox schedule delivery is automatic through the existing main conversation.")


def main_schedule_options(arguments):
    """Reuse the host-side cron tool even when terminal commands execute in Docker."""
    if not main_scheduling_enabled():
        return {}
    validate_main_schedule(arguments)
    return {"deliver": "local", "origin": main_schedule_origin()}


def main_schedule_origin():
    # This marks the main inbox, not a creator-owned delivery destination.
    return {"platform": CRON_ORIGIN, "chat_id": "main"}


def manual_schedule_context(job):
    if is_main_schedule(job):
        return ("Triggered via cronjob(action='run'). The timer admits this prompt into the existing main "
                "conversation; this reports admission, not completion of the task.")
    return ("Triggered via cronjob(action='run'). The job executes in its own fresh cron session; "
            "this reports its outcome.")


def enqueue(prompt, source, event_id, metadata=None):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.events import event_prompt
    if not str(prompt).strip() or not event_id or not source:
        raise ValueError("An event requires nonempty content, source and stable event_id")
    content = event_prompt(prompt, source, event_id, metadata)
    inbox = HomeInbox()
    identity, pending = inbox.enqueue(content)
    if pending:
        from hermes_runtime.adapters.turns import forward_to_active_turn
        try:
            original = dict(inbox.pending()).get(identity)
            if original is not None:
                forward_to_active_turn(original)
        except (OSError, RuntimeError):
            # Admission is already durable. The bound backend will wake it even
            # if the active turn closes between accepting the receipt and IPC.
            logger.info("Event accepted; active-turn handoff deferred to inbox recovery")
    return {"message_id": identity, "pending": pending}


def _admit_scheduled_prompt(prompt, event_id, metadata, *, source="cron"):
    from hermes_runtime.adapters.state import main_gateway_origin, current_home_session_id
    from hermes_runtime.events import event_prompt
    from tools.delegate_project_recovery import publish_wakeup

    # The committed main route owns all proactive replies, regardless of which
    # interface created the timer. Never revive a stale creator-surface pin.
    origin = main_gateway_origin()
    if not origin:
        if source == "cron" and not metadata.get("project_goal"):
            return enqueue(prompt, source, event_id, metadata)
        from hermes_runtime.adapters.inbox import HomeInbox

        identity, _ = HomeInbox().enqueue(event_prompt(prompt, source, event_id, metadata))
        return {"message_id": identity}
    target = current_home_session_id()
    origin = {**origin, "origin_session_id": target}
    if metadata.get("project_id"):
        origin["main_project_event"] = {"source": source, "event_id": event_id, "metadata": metadata}
    if source == "goal" or metadata.get("project_goal"):
        origin["goal_project_id"] = metadata["project_id"]
    identity = f"main-{source}-" + hashlib.sha256(event_id.encode()).hexdigest()
    # The native gateway owns this wake and its reply. A raw session-id wake is
    # claimed by the internal UI and loses the originating messaging transport.
    description = "Run the scheduled prompt in the main conversation" if source == "cron" else "Continue the saved Project Goal"
    publish_wakeup(identity, target, origin, event_prompt(prompt, source, event_id, metadata), goal=description)
    return {"message_id": identity}


def admit_cron(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    """Return an admission receipt, never claim the model has finished the task.

    Only jobs explicitly created for this inbox take this path. Native scripts,
    monitors and other jobs keep their normal execution and delivery semantics.
    """
    from cron.wait_binding import is_wait_timer
    wait_timer = is_wait_timer(job)
    if not wait_timer and not is_main_schedule(job):
        if main_scheduling_enabled():
            raise ValueError("Separate-session cron runs are disabled for managed Hermes. "
                             "Create a main-inbox schedule with cronjob_manage or hermes-autonomous schedule.")
        return None
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.standalone.config import activate_home
    from hermes_constants import get_hermes_home
    validate_main_schedule(job)
    if cancel_event is not None and cancel_event.is_set():
        return False, "Schedule cancelled before admission", "", "cancelled"
    if not enabled():
        activate_home(get_hermes_home())
    request_id = execution_id or job.get("execution_id") or uuid.uuid4().hex
    job["execution_id"] = request_id
    if wait_timer:
        return _admit_wait_timer(job, request_id, cancel_event)
    # The managed validator above still rejects both external monitor inputs.
    from cron.scheduler import _apply_monitor_gate
    job.pop("_managed_monitor_no_change", None)
    job_id = job["id"]
    early, extra_prompt, monitor_context = _apply_monitor_gate(
        job, job_id, job.get("name") or job_id, extra_prompt,
    )
    if early is not None:
        if early[0]:
            job["_managed_monitor_no_change"] = True
        return early
    metadata = {"job_id": job_id, "execution_id": request_id}
    if monitor_context is not None:
        metadata["untrusted_monitor_observation"] = monitor_context
    return _admit_job_prompt(job_id, request_id, "\n\n".join(p for p in (job.get("prompt"), extra_prompt) if p), metadata)


def _admit_wait_timer(job, request_id, cancel_event):
    import sqlite3
    from cron.wait_binding import admitted_binding
    from tools.delegate_project_goal import KEY

    try:
        with admitted_binding(job) as (tag, current, workspace):
            if cancel_event is not None and cancel_event.is_set():
                return False, "Schedule cancelled before admission", "", "cancelled"
            job_id = current["id"]
            metadata = {
                "job_id": job_id, "execution_id": request_id,
                "project_id": tag["project_id"], "wait_id": tag["wait_id"],
            }
            if isinstance(workspace.get(KEY), dict):
                metadata["project_goal"] = True
            return _admit_job_prompt(job_id, request_id, current["prompt"], metadata)
    except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError):
        return False, "External wait timer not admitted", "", "external_wait_not_active"


def _admit_job_prompt(job_id, request_id, prompt, metadata):
    receipt = _admit_scheduled_prompt(prompt, f"{job_id}:{request_id}", metadata)
    response = f"Queued for main: {receipt['message_id']}. Timer handoff saved; see main history for task outcome."
    return True, response + "\n", response, None


async def watch_home(port, token):
    """The bound native backend is the only consumer; reconnect after recoverable errors."""
    from hermes_runtime.adapters.startup import resume_after_bind
    while True:
        try:
            await resume_after_bind(port, token)
        except Exception as error:  # health: allow BLE001 -- transport error text may contain a secret URL
            logger.warning("Autonomous inbox recovery deferred (%s)", type(error).__name__)
        await asyncio.sleep(2)


def schedule(prompt, when, name=None):
    from cron.scheduler import create_job_with_scheduler_registration
    return create_job_with_scheduler_registration(prompt=prompt, schedule=when, name=name, deliver="local",
                                                  origin=main_schedule_origin())
