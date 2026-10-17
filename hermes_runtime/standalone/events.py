"""Mail and native schedule admissions share the existing durable main inbox."""

import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)
CRON_ORIGIN = "hermes-autonomous"


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


def admit_cron(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    """Return an admission receipt, never claim the model has finished the task.

    Only jobs explicitly created for this inbox take this path. Native scripts,
    monitors and other jobs keep their normal execution and delivery semantics.
    """
    if (job.get("origin") or {}).get("platform") != CRON_ORIGIN:
        return None
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.standalone.config import activate_home
    from hermes_constants import get_hermes_home
    if any(job.get(k) for k in ("script", "monitor_script", "monitor_url", "context_from", "skills", "skill",
                                "model", "provider", "base_url", "enabled_toolsets", "workdir",
                                "reasoning_effort", "interpreter")):
        raise ValueError("Main-inbox schedules accept prompts; use a normal Hermes job for script/model overrides")
    if cancel_event is not None and cancel_event.is_set():
        return False, "Schedule cancelled before admission", "", "cancelled"
    if not enabled():
        activate_home(get_hermes_home())
    request_id = execution_id or job.get("execution_id") or uuid.uuid4().hex
    job["execution_id"] = request_id
    receipt = enqueue("\n\n".join(p for p in (job.get("prompt"), extra_prompt) if p),
                      "cron", f"{job['id']}:{request_id}", {"job_id": job["id"], "execution_id": request_id})
    return True, f"Accepted into main-session inbox: {receipt['message_id']}\n", "", None


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
    from cron.jobs import create_job
    return create_job(prompt=prompt, schedule=when, name=name, deliver="local",
                      origin={"platform": CRON_ORIGIN, "chat_id": "main"})
