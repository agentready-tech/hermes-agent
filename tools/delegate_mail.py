"""Model-facing coordination on the existing delegate_task surface."""

import json
import threading
import time

from tools.delegate_mail_store import MailStore, pending_mail_rows


def mail_action(actor, action, recipient=None, message=None, event_id=None, timeout_seconds=30):
    with MailStore(actor) as store:
        if action == "peers":
            return {"self_id": store.self_id, "parent_id": store.parent(store.self_id), "agents": store.peers()}
        if action == "message":
            result = store.send(recipient, message, event_id)
            publish_pending_mail()
            return result
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be a number from 0 to 60")
        deadline = time.monotonic() + timeout_seconds
        stopped = threading.Event()
        while True:
            store.acknowledge_recorded()
            pending = store.pending()
            if pending:
                return {"status": "messages_pending", "message_ids": [p["message_id"] for p in pending],
                        "note": "Messages enter this conversation after this tool returns; continue your task."}
            if getattr(actor, "_interrupt_requested", False):
                return {"status": "interrupted"}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "timeout"}
            stopped.wait(min(remaining, 0.1))


def publish_pending_mail():
    from tools.delegate_project_store import _transaction
    from tools.delegate_project_recovery import publish_wakeup
    with _transaction() as conn:
        rows = pending_mail_rows(conn)
    count = 0
    for row in rows:
        origin = json.loads(row['origin_json'])
        if origin.get("wake"):
            identity = row['message_id']
            created = publish_wakeup(identity, row['root_session_id'], origin,
                f"Coordination mail is waiting for agent {row['target_session_id']}. Resume existing project work and handle the saved "
                "mailbox. This is an intermediate message, not a subagent finishing its task.", agent_message=True)
            count += bool(created)
            if created:
                from tools.process_registry import process_registry
                with _transaction() as conn:
                    raw_event = conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id=?", (identity,)).fetchone()[0]
                process_registry.completion_queue.put(json.loads(raw_event))
    return count
