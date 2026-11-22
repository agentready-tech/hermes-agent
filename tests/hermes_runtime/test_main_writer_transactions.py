"""Main inbox writes can participate in the owning profile's transaction."""

import json

import pytest

from hermes_runtime.adapters.inbox import HomeInbox
from hermes_runtime.events import event_prompt
from tools.delegate_project_recovery import _write_wakeup_in_transaction

pytest_plugins = ['tests.hermes_runtime.test_mail_reply_route']


class _AbortTransaction(Exception):
    pass


@pytest.mark.parametrize("route", ["home", "gateway"])
def test_main_writer_leaves_commit_and_rollback_to_supplied_cursor_owner(tmp_path, route):
    inbox = HomeInbox(tmp_path)
    content = event_prompt("Inert event", "fixture", "fixture-1", None)
    key, identity = inbox._enqueue_identity(content, "fixture-1")
    origin = {"session_key": "agent:main", "chat_id": "fixture-chat", "thread_id": "fixture-topic"}

    def stage(cursor):
        if route == "home":
            assert inbox._enqueue_in_transaction(cursor, content, key=key, identity=identity) == (identity, True)
        else:
            assert _write_wakeup_in_transaction(cursor, identity, "fixture-main", origin, content,
                                                agent_message=False, goal="Inert event")

    def rows(conn):
        home = conn.execute("SELECT message_id,content FROM agentready_inbox").fetchall()
        wake = conn.execute("SELECT delegation_id,origin_session,delivery_state,event_json,result_json "
                            "FROM async_delegations").fetchall()
        return [tuple(row) for row in home], [tuple(row) for row in wake]

    def assert_staged(conn):
        home, wake = rows(conn)
        if route == "home":
            assert home == [(identity, json.dumps(content, ensure_ascii=False))]
            assert wake == []
        else:
            assert home == []
            [(wake_id, session_key, state, raw_event, raw_result)] = wake
            assert (wake_id, session_key, state) == (identity, origin["session_key"], "pending")
            event = json.loads(raw_event)
            assert {key: event[key] for key in origin} == origin
            assert event["summary"] == json.loads(raw_result)["summary"] == content

    with pytest.raises(_AbortTransaction):
        with inbox.transaction() as conn:
            stage(conn.cursor())
            assert_staged(conn)
            raise _AbortTransaction
    with inbox.transaction() as conn:
        assert rows(conn) == ([], [])
        stage(conn.cursor())
    with HomeInbox(tmp_path).transaction() as reopened:
        assert_staged(reopened)


@pytest.mark.parametrize('limit', ['completed', 'pending'])
def test_prune_preserves_pending_mail_payload_but_not_other_old_completions(telegram_home, monkeypatch, limit):
    from queue import Queue

    from hermes_runtime.sessions import HOME_SESSION_KEY
    from hermes_runtime.standalone.workspace import admit_mail
    from tools import async_delegation as ledger
    from tools.delegate_project_recovery import publish_wakeup

    home, _ = telegram_home
    inbox = HomeInbox(home)
    settled = admit_mail('Settled mail', 'gmail:fixture', 'settled')['message_id']
    ledger.mark_completion_delivered(settled)
    pending = admit_mail('Pending mail', 'gmail:fixture', 'pending')['message_id']
    for identity in ('fixture-old-completion', 'fixture-new-completion'):
        assert publish_wakeup(identity, 'agentready_home', {'session_key': HOME_SESSION_KEY}, 'Inert completion')
    with inbox.transaction() as conn:
        conn.execute('UPDATE async_delegations SET updated_at=updated_at-1000 WHERE delegation_id IN (?,?)',
                     (pending, 'fixture-old-completion'))
        original = conn.execute('SELECT event_json FROM async_delegations WHERE delegation_id=?', (pending,)).fetchone()[0]
    monkeypatch.setattr(ledger, '_MAX_RETAINED_COMPLETED', 1 if limit == 'completed' else 100)
    monkeypatch.setattr(ledger, '_MAX_DURABLE_PENDING', 1 if limit == 'pending' else 100)
    ledger._prune_durable_records()
    with inbox.transaction() as conn:
        rows = {r[0]: tuple(r[1:]) for r in conn.execute('SELECT delegation_id,delivery_state,event_json FROM async_delegations')}
    assert rows[pending] == ('pending', original)
    assert 'fixture-old-completion' not in rows and 'fixture-new-completion' in rows
    assert (settled in rows) == (limit == 'pending')

    queue = Queue()
    assert ledger.restore_undelivered_completions(queue) == 2
    recovered = [queue.get_nowait(), queue.get_nowait()]
    event, = [row for row in recovered if row['delegation_id'] == pending]
    assert event == {**json.loads(original), 'restored': True}
    claim = ledger.claim_event_delivery(event, 'fixture-consumer')
    assert claim and ledger.complete_completion_delivery(pending, claim)
    assert admit_mail('Not a replacement', 'gmail:fixture', 'pending') == {'message_id': pending}