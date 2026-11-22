"""Managed mailbox ingress into the existing durable home inbox."""

import json
import logging
import signal
import threading
from functools import partial
from pathlib import Path

logger = logging.getLogger(__name__)


def admit_mail(prompt, source, event_id, metadata=None, *, session_store=None):
    """Admit mail called directly inside an already bound managed profile."""
    from hermes_runtime.adapters.hermes import managed_home

    return _admit_profile_mail(managed_home(), prompt, source, event_id, metadata, session_store=session_store)


def _admit_profile_mail(home, prompt, source, event_id, metadata=None, *, session_store=None):
    from hermes_runtime.standalone.main_admission import WorkspaceMail, admit_main

    return admit_main(home, WorkspaceMail(prompt, source, event_id, metadata, session_store))


def admit_batches(home, response, cursors, *, admit=None, save=None):
    """Commit the cursor only after every event has a durable native receipt.

    A crash after admission but before saving replays the batch through the native
    receipt's stable identity. A failed admission never retires the remainder.
    """
    from utils import atomic_json_write

    admit = admit or partial(_admit_profile_mail, home)
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


def poll(home, config, cursors, *, admit=None, stopped=None):
    if config.get("native") is not True:
        return cursors
    from hermes_runtime.workspace.service import Workspace
    from hermes_runtime.workspace.transport import poll_request
    response = Workspace(home, runner=poll_request).events(cursors)
    if stopped is not None and stopped.is_set():
        return cursors
    return admit_batches(home, response, cursors, admit=admit)


def worker(home):
    from hermes_runtime.standalone.config import activate_home

    activate_home(home)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    run_polls(home, stopped)


def run_polls(home, stopped, *, session_store=None):
    from hermes_runtime.standalone.config import settings

    path = Path(home) / "autonomous" / "workspace-cursors.json"
    cursors = None
    admit = partial(_admit_profile_mail, home, session_store=session_store)
    while not stopped.is_set():
        interval = 60
        try:
            config = settings(home).get("workspace", {})
            interval = max(30, float(config.get("poll_seconds", 60)))
            if config.get("native") is True:
                if cursors is None:
                    cursors = json.loads(path.read_text()) if path.exists() else {}
                poll(home, config, cursors, admit=admit, stopped=stopped)
        except Exception as error:  # health: allow BLE001 -- transport exceptions can contain credentials
            logger.warning("Workspace poll deferred (%s); batch remains retryable", type(error).__name__)
        stopped.wait(interval)


def run_profile_polls(authority, stopped):
    from gateway.session_authorities import owner_scope

    with owner_scope(authority):
        run_polls(Path(authority.profile_id), stopped, session_store=authority.runner.session_store)
