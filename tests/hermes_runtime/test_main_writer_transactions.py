"""Main inbox writes can participate in the owning profile's transaction."""

import json

import pytest

from hermes_runtime.adapters.inbox import HomeInbox
from hermes_runtime.events import event_prompt
from tools.delegate_project_recovery import _write_wakeup_in_transaction


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