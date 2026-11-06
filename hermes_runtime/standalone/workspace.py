"""Managed mailbox ingress into the existing durable home inbox."""

import hashlib
import json
import logging
import signal
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def admit_mail(prompt, source, event_id, metadata=None):
    """Let the native gateway own a mail reply, including while Panel owns a turn.

    Injecting into HomeInbox would lend the mail the foreground UI's transport.
    The existing durable gateway admission instead runs once in the same main
    history and keeps its committed messaging destination through retries.
    """
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.state import main_gateway_origin, current_home_session_id
    from hermes_runtime.events import event_prompt
    from hermes_runtime.standalone.events import enqueue
    from tools.delegate_project_recovery import publish_wakeup

    if not str(prompt).strip() or not event_id or not source:
        raise ValueError("An event requires nonempty content, source and stable event_id")
    key = json.dumps([source, event_id])
    with HomeInbox().transaction() as conn:
        legacy = conn.execute("SELECT message_id,delivered_at FROM agentready_inbox WHERE event_key=?", (key,)).fetchone()
    if legacy is not None:
        # A cursor-write retry across an upgrade must not execute an old mail again.
        return {"message_id": legacy[0], "pending": legacy[1] is None}
    origin = main_gateway_origin()
    if not origin:
        return enqueue(prompt, source, event_id, metadata)
    identity = "main-mail-" + hashlib.sha256(key.encode()).hexdigest()
    publish_wakeup(identity, current_home_session_id(), origin, event_prompt(prompt, source, event_id, metadata),
                   goal="Process the incoming email in the existing main conversation")
    return {"message_id": identity}


def admit_batches(home, response, cursors, *, admit=None, save=None):
    """Commit the cursor only after every event has a durable native receipt.

    A crash after admission but before saving replays the batch through the native
    receipt's stable identity. A failed admission never retires the remainder.
    """
    from utils import atomic_json_write

    admit = admit or admit_mail
    save = save or (lambda value: atomic_json_write(
        Path(home) / "autonomous" / "workspace-cursors.json", value, mode=0o600, fsync_dir=True))
    for batch in response.get("batches", []):
        if batch.get("error"):
            continue
        for event in batch["events"]:
            admit(event["prompt"], event["source"], event["id"], event.get("metadata"))
        updated = {**cursors, batch["connectionId"]: batch["cursor"]}
        save(updated)
        cursors.update(updated)
    return cursors


def poll(home, config, cursors):
    if config.get("native") is not True:
        return cursors
    from hermes_runtime.workspace.service import Workspace
    return admit_batches(home, Workspace(home).events(cursors), cursors)


def worker(home):
    from hermes_runtime.standalone.config import activate_home, settings

    activate_home(home)
    path = Path(home) / "autonomous" / "workspace-cursors.json"
    cursors = json.loads(path.read_text()) if path.exists() else {}
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    while not stopped.is_set():
        config = settings(home).get("workspace", {})
        if config.get("native") is not True:
            stopped.wait(max(30, float(config.get("poll_seconds", 60))))
            continue
        try:
            poll(home, config, cursors)
        except Exception as error:  # health: allow BLE001 -- transport exceptions can contain credentials
            logger.warning("Workspace poll deferred (%s); batch remains retryable", type(error).__name__)
        stopped.wait(max(30, float(config.get("poll_seconds", 60))))
