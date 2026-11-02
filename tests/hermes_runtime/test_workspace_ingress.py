"""A managed mailbox cannot advance past work lacking a durable inbox receipt."""

import json

import pytest

from hermes_runtime.standalone.workspace import admit_batches
from hermes_runtime.adapters.inbox import HomeInbox
from hermes_runtime.standalone.config import activate_home


def batch():
    return {"batches": [{"connectionId": "mail-fixture", "cursor": {"uid": 3},
        "events": [{"id": str(n), "source": "email:fixture", "prompt": "Synthetic email", "metadata": {}} for n in (1, 2, 3)]}]}


def test_partial_admission_and_failed_cursor_write_replay_safely(tmp_path):
    cursors, accepted, writes = {}, set(), []

    def fail_second(prompt, source, identity, metadata):
        if identity == "2":
            raise OSError("Synthetic interruption")
        accepted.add(identity)

    with pytest.raises(OSError):
        admit_batches(tmp_path, batch(), cursors, admit=fail_second, save=writes.append)
    assert cursors == {} and writes == [] and accepted == {"1"}
    with pytest.raises(OSError):
        admit_batches(tmp_path, batch(), cursors, admit=lambda *args: accepted.add(args[2]),
            save=lambda value: (_ for _ in ()).throw(OSError("Synthetic disk failure")))
    assert cursors == {} and accepted == {"1", "2", "3"}
    admit_batches(tmp_path, batch(), cursors, admit=lambda *args: accepted.add(args[2]), save=writes.append)
    assert cursors == {"mail-fixture": {"uid": 3}} and len(accepted) == 3


def test_real_home_inbox_deduplicates_replayed_mail(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    activate_home(home)
    monkeypatch.setattr("hermes_runtime.adapters.turns.forward_to_active_turn", lambda *args: None)
    admit_batches(home, batch(), {}, save=lambda value: None)
    admit_batches(home, batch(), {}, save=lambda value: None)
    with HomeInbox().transaction() as conn:
        keys = [json.loads(row[0]) for row in conn.execute("SELECT event_key FROM agentready_inbox")]
    assert sum(key[0] == "email:fixture" for key in keys) == 3
