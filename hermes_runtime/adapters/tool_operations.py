"""Durable receipts for in-flight tools. Only the conversation owner writes history.

Execution and delivery are separate: a missing process leaves an unknown outcome,
never an implicit retry of an external side effect. Completion uses the existing
home inbox, whose receipt is retired only after canonical history is committed.
"""

import json
import logging
import os
import time
import uuid

import psutil

from hermes_runtime.adapters.inbox import HomeInbox
from hermes_runtime.events import event_prompt

logger = logging.getLogger(__name__)


def _capture_origin():
    """Freeze the dispatching chat, never infer it from the latest home event."""
    from gateway.session_context import get_session_env
    fields = ("platform", "chat_id", "chat_type", "thread_id", "user_id", "user_name", "scope_id", "profile")
    origin = {key: value for key in fields if (value := get_session_env("HERMES_SESSION_" + key.upper(), ""))}
    if not all(origin.get(key) for key in ("platform", "chat_id", "chat_type")) or origin["platform"] in {"local", "api_server"}:
        return {}
    from gateway.config import Platform
    from gateway.session import SessionSource, build_session_key
    source = SessionSource(**{**origin, "platform": Platform(origin["platform"])})
    # Native automation admission needs the registered logical home route.
    # The explicit platform/chat fields retain the dispatching transport.
    origin["session_key"] = get_session_env("HERMES_SESSION_KEY", "") or build_session_key(source, profile=source.profile)
    return origin


def _owner_alive(pid, birth):
    try:
        return psutil.Process(pid).create_time() == birth
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # Unknown ownership is not evidence of death.


class ToolOperationStore:
    def __init__(self, home):
        self.home = home
        self.inbox = HomeInbox(home)

    def admit(self, session_id, uid, call_id, name, args, resource, group, *, internal=False):
        """Reserve identity and resource before dispatch. A replay never executes twice."""
        with self.inbox.transaction() as conn:
            row = conn.execute("SELECT * FROM tool_operations WHERE tool_call_uid=?", (uid,)).fetchone()
            if row:
                return dict(row), False
            busy = conn.execute("SELECT operation_id FROM tool_operations WHERE resource_key=? "
                "AND group_id<>? AND state='running' LIMIT 1", (resource, group)).fetchone()
            if busy:
                return {"status": "not_started", "waiting_for": busy[0],
                    "message": "This resource is still in use. Continue after its completion event."}, False
            identity = "op-" + uuid.uuid4().hex
            conn.execute("INSERT INTO tool_operations(operation_id,session_id,tool_call_uid,tool_call_id,tool_name,"
                "arguments_json,origin_json,resource_key,group_id,owner_pid,owner_birth,state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (identity, session_id, uid, call_id, name,
                json.dumps(args, ensure_ascii=False), json.dumps(_capture_origin()), resource, group, os.getpid(),
                psutil.Process().create_time(), "running", time.time()))
            if internal:
                # Nested code steps belong to their script's result, not separate
                # model/inbox turns. They still acquire the ordinary resource lease.
                conn.execute("UPDATE tool_operations SET delivered_at=? WHERE operation_id=?", (time.time(), identity))
            return dict(conn.execute("SELECT * FROM tool_operations WHERE operation_id=?", (identity,)).fetchone()), True

    def get(self, identity):
        with self.inbox.transaction() as conn:
            row = conn.execute("SELECT * FROM tool_operations WHERE operation_id=?", (identity,)).fetchone()
            return dict(row) if row else None

    def complete(self, identity, result, *, failed=False):
        with self.inbox.transaction() as conn:
            conn.execute("UPDATE tool_operations SET state=?,result_json=?,finished_at=? "
                "WHERE operation_id=? AND state='running'", ("failed" if failed else "completed",
                json.dumps(result, ensure_ascii=False), time.time(), identity))
        self.publish(identity)

    def defer(self, identity):
        with self.inbox.transaction() as conn:
            conn.execute("UPDATE tool_operations SET deferred=1 WHERE operation_id=?", (identity,))
        self.publish(identity)

    def publish(self, identity):
        row = self.get(identity)
        if not row or not row["deferred"] or row["state"] == "running" or row["delivered_at"]:
            return
        result = json.loads(row["result_json"]) if row["result_json"] else None
        prompt = json.dumps({"operation_id": identity, "tool": row["tool_name"],
            "tool_call_id": row["tool_call_id"], "session_id": row["session_id"],
            "status": row["state"], "result": result}, ensure_ascii=False)
        content = event_prompt(prompt, "tool_operation", identity,
            {"session_id": row["session_id"], "origin": json.loads(row["origin_json"])})
        _, pending = self.inbox.enqueue(content)
        if pending:
            from hermes_runtime.adapters.turns import forward_to_active_turn
            try:
                forwarded = forward_to_active_turn(content)
            except (OSError, RuntimeError):
                logger.debug("Tool completion remains in the durable inbox", exc_info=True)
                forwarded = None
            if not forwarded:
                # The worker is still in the dispatching Gateway process. Offer
                # its durable wake there now, rather than waiting for another
                # process's orphan sweep to discover the saved completion.
                from hermes_constants import set_hermes_home_override, reset_hermes_home_override
                from hermes_runtime.adapters.startup import _wake_inbox
                from tools.async_delegation import restore_undelivered_completions
                from tools.process_registry import process_registry
                token = set_hermes_home_override(self.home)
                try:
                    _wake_inbox(self.inbox, reconcile=False)
                    restore_undelivered_completions(process_registry.completion_queue)
                finally:
                    reset_hermes_home_override(token)
        else:
            with self.inbox.transaction() as conn:
                conn.execute("UPDATE tool_operations SET delivered_at=? WHERE operation_id=?", (time.time(), identity))

    def recover(self, *, finished_group=None):
        """Reconcile canonical results, dead workers and the completion/outbox crash window."""
        self.inbox.acknowledge_recorded()
        with self.inbox.transaction() as conn:
            rows = conn.execute("SELECT * FROM tool_operations WHERE delivered_at IS NULL OR state='running'").fetchall()
            for row in rows:
                if row["state"] != "running" and not row["deferred"] and conn.execute(
                    "SELECT 1 FROM messages WHERE role='tool' AND tool_call_uid=? LIMIT 1",
                    (row["tool_call_uid"],)).fetchone():
                    conn.execute("UPDATE tool_operations SET delivered_at=? WHERE operation_id=?",
                        (time.time(), row["operation_id"]))
                elif row["state"] == "running" and not _owner_alive(row["owner_pid"], row["owner_birth"]):
                    recovery = {"message": "The executing process stopped. Check the external outcome before retrying; "
                        "this operation was not rerun."}
                    if row["tool_name"] == "execute_code":
                        run = conn.execute("SELECT run_id FROM code_execution_runs WHERE tool_call_uid=? AND session_id=?",
                            (row["tool_call_uid"], row["session_id"])).fetchone()
                        if run:
                            recovery.update(resume_run_id=run[0], message="Resume with execute_code(resume_run_id=...). "
                                "Saved tool results are reused; an uncertain step is never repeated automatically.")
                    conn.execute("UPDATE tool_operations SET state='outcome_unknown',deferred=1,"
                        "result_json=?,finished_at=? WHERE operation_id=?", (json.dumps(recovery), time.time(), row["operation_id"]))
                elif row["state"] != "running" and (row["group_id"] == finished_group
                        or not _owner_alive(row["owner_pid"], row["owner_birth"])):
                    # A completed worker may die before the normal tool result was committed.
                    conn.execute("UPDATE tool_operations SET deferred=1 WHERE operation_id=?", (row["operation_id"],))
        for row in rows:
            self.publish(row["operation_id"])

    def has_running(self, session_id):
        with self.inbox.transaction() as conn:
            return bool(conn.execute("SELECT 1 FROM tool_operations WHERE session_id=? AND state='running' LIMIT 1",
                (session_id,)).fetchone())
