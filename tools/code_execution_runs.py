"""Script receipts and replay of managed tool steps in the existing state.db.

Python itself is rerun from the top. Only recorded tool calls are replayed;
ordinary Python outside those calls must be replay-safe and self-contained.
An uncertain external effect is deliberately not retried.
"""

import json
import os
import time
import uuid

import psutil

from hermes_constants import get_hermes_home
from hermes_runtime.adapters.tool_operations import ToolOperationStore, _owner_alive


class CodeRun:
    def __init__(self, session_id, code, uid=None, resume_run_id=None):
        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = ToolOperationStore(home)
        self.session_id = session_id
        self.ordinal = 0
        self.used_steps = set()
        self.request_steps = {}
        with self.store.inbox.transaction() as conn:
            if resume_run_id:
                row = conn.execute("SELECT * FROM code_execution_runs WHERE run_id=? AND session_id=?",
                    (resume_run_id, session_id)).fetchone()
                if not row:
                    raise ValueError("Unknown code run in this session")
                if row["state"] == "running" and _owner_alive(row["owner_pid"], row["owner_birth"]):
                    raise ValueError("This code run is still running; wait for its completion")
                if code and code != row["code"]:
                    raise ValueError("Resume must use the saved script unchanged")
                self.run_id, self.code = row["run_id"], row["code"]
                self.cached_result = row["result_json"] if row["state"] == "completed" else None
                if not self.cached_result:
                    conn.execute("UPDATE code_execution_runs SET state='running',owner_pid=?,owner_birth=?,"
                        "tool_call_uid=COALESCE(?,tool_call_uid) WHERE run_id=?",
                        (os.getpid(), psutil.Process().create_time(), uid, self.run_id))
            else:
                self.run_id, self.code, self.cached_result = "code-" + uuid.uuid4().hex, code, None
                conn.execute("INSERT INTO code_execution_runs(run_id,session_id,tool_call_uid,code,state,owner_pid,owner_birth,created_at) "
                    "VALUES (?,?,?,?,'running',?,?,?)", (self.run_id, session_id, uid, code,
                    os.getpid(), psutil.Process().create_time(), time.time()))

    def call(self, name, args, invoke, *, step=None, request_id=None):
        from agent.tool_operation_lifecycle import resource_key
        key = self.request_steps.get(request_id) if request_id else None
        if key is None:
            self.ordinal += 1
            key = "named:" + step if step else "ordinal:" + str(self.ordinal)
            if key in self.used_steps:
                raise ValueError("A named step may occur only once per run; include the item ID in loop step names")
            self.used_steps.add(key)
            if request_id:
                self.request_steps[request_id] = key
        uid = self.run_id + ":" + key
        row, execute = self.store.admit(self.session_id, uid, uid, name, args, resource_key(name),
            self.run_id, internal=True)
        if not execute:
            if "operation_id" not in row:
                raise RuntimeError("Resource busy; resume this code run after its owner's completion")
            if row["tool_name"] != name or json.loads(row["arguments_json"]) != args:
                raise RuntimeError("Replay diverged: a saved step has different tool/arguments; no call executed")
            if row["state"] in {"completed", "failed"}:
                return json.loads(row["result_json"])
            raise RuntimeError("Step outcome is unknown or still running; verify the external result before retrying")
        try:
            result = invoke(name, args, uid)
        except Exception as exc:
            # The handler may have committed a remote write before raising.
            with self.store.inbox.transaction() as conn:
                conn.execute("UPDATE tool_operations SET state='outcome_unknown',result_json=?,finished_at=? WHERE operation_id=?",
                    (json.dumps({"error": str(exc)}), time.time(), row["operation_id"]))
            raise
        self.store.complete(row["operation_id"], result)
        return result

    def finish(self, result):
        body = json.loads(result)
        body["code_run_id"] = self.run_id
        body["recovery"] = "Resume unchanged with resume_run_id; managed steps replay saved results. Python outside tool calls must be replay-safe."
        value = json.dumps(body, ensure_ascii=False)
        state = "completed" if body.get("status") == "success" and not body.get("tool_errors") else "stopped"
        with self.store.inbox.transaction() as conn:
            conn.execute("UPDATE code_execution_runs SET state=?,result_json=? WHERE run_id=?", (state, value, self.run_id))
        return value
