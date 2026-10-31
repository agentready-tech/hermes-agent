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
        raise ValueError("Main-inbox schedules return to the main conversation; put any other delivery "
                         "destination in the prompt for the main agent to handle.")


def main_schedule_options(arguments):
    """Reuse the host-side cron tool even when terminal commands execute in Docker."""
    if not main_scheduling_enabled():
        return {}
    validate_main_schedule(arguments)
    return {"deliver": "local", "origin": main_schedule_origin()}


def main_schedule_origin():
    from gateway.session_context import get_session_env
    from hermes_runtime.adapters.state import main_gateway_origin

    platform = str(get_session_env("HERMES_SESSION_PLATFORM") or "")
    chat = str(get_session_env("HERMES_SESSION_CHAT_ID") or "")
    origin = main_gateway_origin() if platform and chat else {}
    if origin and origin.get("platform") == platform and origin.get("chat_id") == chat:
        reply_to = {"kind": "gateway", "origin": origin}
    else:
        reply_to = {"kind": "main"}
    return {"platform": CRON_ORIGIN, "chat_id": "main", "reply_to": reply_to}


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


def _admit_scheduled_prompt(prompt, event_id, metadata, reply_to):
    from hermes_runtime.adapters.state import main_gateway_origin, current_home_session_id
    from hermes_runtime.events import event_prompt
    from tools.delegate_project_recovery import publish_wakeup

    # Old jobs predate reply routing. New jobs explicitly retain their creator's
    # surface, so a web-created timer never switches to the last Telegram chat.
    origin = main_gateway_origin() if reply_to is None else reply_to.get("origin", {})
    if not origin:
        return enqueue(prompt, "cron", event_id, metadata)
    current = main_gateway_origin()
    if any(origin.get(key) != current.get(key) for key in ("platform", "chat_id", "thread_id", "session_key")):
        raise ValueError("The scheduled reply route is no longer bound to this main conversation; update the job from its original chat")
    origin = {**origin, "origin_session_id": current_home_session_id()}
    identity = "main-cron-" + hashlib.sha256(event_id.encode()).hexdigest()
    # The native gateway owns this wake and its reply. A raw session-id wake is
    # claimed by the internal UI and loses the originating messaging transport.
    publish_wakeup(identity, current_home_session_id(), origin, event_prompt(prompt, "cron", event_id, metadata),
                   goal="The scheduled time has arrived; execute this task in the main conversation")
    return {"message_id": identity}


def admit_cron(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    """Return an admission receipt, never claim the model has finished the task.

    Only jobs explicitly created for this inbox take this path. Native scripts,
    monitors and other jobs keep their normal execution and delivery semantics.
    """
    if not is_main_schedule(job):
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
    receipt = _admit_scheduled_prompt("\n\n".join(p for p in (job.get("prompt"), extra_prompt) if p),
                                     f"{job['id']}:{request_id}", {"job_id": job["id"], "execution_id": request_id},
                                     (job.get("origin") or {}).get("reply_to"))
    response = f"Accepted into main-session inbox: {receipt['message_id']}. Task execution is pending in the main conversation."
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
