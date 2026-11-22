"""Mailbox results share the main transcript and its committed messaging transport."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.hermes_runtime.test_scheduled_reply_route import telegram_home as scheduled_telegram_home


@pytest.fixture
def telegram_home(monkeypatch):
    yield from scheduled_telegram_home.__wrapped__(monkeypatch)


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


def test_settled_local_mail_receipt_survives_reopen_and_new_gateway_origin(telegram_home):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.state import main_gateway_origin
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.workspace import admit_mail
    from hermes_state import SessionDB

    home, entry = telegram_home
    entry.suspended = True
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("UPDATE gateway_routing SET entry_json=?", (json.dumps(entry.to_dict()),))
    original = admit_mail("Inert mail", "email:fixture", "mail-route-switch")
    db = SessionDB(db_path=home / "state.db")
    try:
        db.append_message(HOME_SESSION_ID, "user", "Inert mail", platform_message_id=original["message_id"])
    finally:
        db.close()
    HomeInbox(home).acknowledge_recorded()

    entry.suspended = False
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("UPDATE gateway_routing SET entry_json=?", (json.dumps(entry.to_dict()),))
    assert main_gateway_origin()["thread_id"] == "77"
    replay = admit_mail("Inert mail", "email:fixture", "mail-route-switch")
    assert replay == {"message_id": original["message_id"], "pending": False}
    assert HomeInbox(home).pending() == []
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT count(*) FROM async_delegations").fetchone()[0] == 0


@pytest.mark.parametrize('settled', [False, True])
def test_gateway_receipt_keeps_original_origin_after_route_loss(telegram_home, settled):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.workspace import admit_mail
    from tools.async_delegation import mark_completion_delivered

    home, entry = telegram_home
    original = admit_mail('Inert gateway mail', 'gmail:fixture', 'saved-route')
    if settled:
        mark_completion_delivered(original['message_id'])
    entry.suspended = True
    with HomeInbox(home).transaction() as conn:
        conn.execute('UPDATE gateway_routing SET entry_json=?', (json.dumps(entry.to_dict()),))
    assert admit_mail('Not a replacement', 'gmail:fixture', 'saved-route') == original
    with HomeInbox(home).transaction() as conn:
        row, = conn.execute('SELECT delivery_state,event_json FROM async_delegations').fetchall()
        event = json.loads(row[1])
        assert row[0] == ('delivered' if settled else 'pending')
        assert (event['chat_id'], event['thread_id']) == ('42', '77')
        assert 'Inert gateway mail' in event['summary']
    assert not HomeInbox(home).pending()


def test_concurrent_mail_uses_one_branch_when_callers_see_different_origins(telegram_home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.workspace import admit_mail

    home, entry = telegram_home
    handoffs = []
    monkeypatch.setattr('hermes_runtime.adapters.turns.forward_to_active_turn', handoffs.append)
    barrier = Barrier(2)

    def write(view):
        token = set_hermes_home_override(str(home))
        try:
            barrier.wait(timeout=10)
            return admit_mail('Inert concurrent mail', 'email:fixture', 'overlap',
                              session_store=SimpleNamespace(lookup_by_session_key=lambda _: view))
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, entry)
        second = executor.submit(write, replace(entry, suspended=True))
        receipt = first.result(timeout=15)
        assert second.result(timeout=15) == receipt
    pending = HomeInbox(home).pending()
    with HomeInbox(home).transaction() as conn:
        wake = conn.execute('SELECT delegation_id,event_json FROM async_delegations').fetchall()
    assert len(pending) + len(wake) == 1
    expected = pending[0][0] if pending else wake[0][0]
    assert receipt['message_id'] == expected
    assert len(handoffs) == len(pending)
    # Changing the route again cannot revisit either writer or the handoff.
    assert admit_mail('Not a replacement', 'email:fixture', 'overlap') == receipt
    assert len(handoffs) == len(pending)
