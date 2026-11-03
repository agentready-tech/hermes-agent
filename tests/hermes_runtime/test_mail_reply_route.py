"""Mailbox results share the main transcript and its committed messaging transport."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.hermes_runtime.test_scheduled_reply_route import telegram_home


@pytest.mark.parametrize("source", ["gmail:fixture", "email:fixture"])
@pytest.mark.parametrize("busy", [False, True])
def test_mail_retains_gateway_route_without_joining_a_panel_turn(telegram_home, source, busy):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.turns import ActiveHomeTurn
    from hermes_runtime.sessions import HOME_SESSION_KEY
    from hermes_runtime.standalone.workspace import admit_batches
    from tui_gateway import server

    home, _ = telegram_home
    response = {"batches": [{"connectionId": "fixture", "cursor": {"uid": 1}, "events": [
        {"id": "mail-1", "source": source, "prompt": "Synthetic incoming email", "metadata": {
            "origin": {"platform": "telegram", "chat_id": "untrusted-destination"}}}]}]}
    turn = ActiveHomeTurn() if busy else None
    try:
        cursors = {}
        admit_batches(home, response, cursors)
        admit_batches(home, response, cursors)  # cursor-write retry must not re-run the model
        with sqlite3.connect(home / "state.db") as conn:
            events = [json.loads(row[0]) for row in conn.execute("SELECT event_json FROM async_delegations")]
        assert len(events) == 1
        event = events[0]
        assert event["session_key"] == HOME_SESSION_KEY
        assert (event["platform"], event["chat_id"], event["thread_id"]) == ("telegram", "42", "77")
        assert "[UNTRUSTED EVENT DATA]" in event["summary"] and "Synthetic incoming email" in event["summary"]
        assert not server._session_owns_notification_event("panel", {
            "agent": SimpleNamespace(session_id="agentready_home"), "session_key": "agentready_home"}, event)
        assert HomeInbox().pending() == []
        if turn:
            assert turn.take() == []
        assert cursors == {"fixture": {"uid": 1}}
    finally:
        if turn:
            turn.finish(error="fixture finished")


@pytest.mark.parametrize("legacy", [False, True])
def test_mail_keeps_local_fallback_and_does_not_replay_legacy_receipts(telegram_home, legacy):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.events import event_prompt
    from hermes_runtime.standalone.workspace import admit_batches

    home, entry = telegram_home
    inbox = HomeInbox()
    if legacy:
        inbox.enqueue(event_prompt("Already admitted email", "gmail:fixture", "mail-1", {}))
    else:
        entry.suspended = True
        with sqlite3.connect(home / "state.db") as conn:
            conn.execute("UPDATE gateway_routing SET entry_json=?", (json.dumps(entry.to_dict()),))
    response = {"batches": [{"connectionId": "fixture", "cursor": {"uid": 1}, "events": [
        {"id": "mail-1", "source": "gmail:fixture", "prompt": "Synthetic email", "metadata": {}}]}]}
    admit_batches(home, response, {})
    admit_batches(home, response, {})
    assert len(inbox.pending()) == 1
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT count(*) FROM async_delegations").fetchone()[0] == 0
