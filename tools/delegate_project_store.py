"""Persistent project identity and inbox; native sessions remain the history authority."""

import json
import os
import re
import sqlite3
import time
import uuid

from tools.async_delegation import _connect, _owner_liveness


def _transaction():
    from hermes_cli.sqlite_util import transaction
    return transaction(_connect(), immediate=True)


def write_workspace(conn, subagent_id, workspace, change):
    result = change(workspace, conn)
    conn.execute("UPDATE delegated_projects SET workspace_json=? WHERE subagent_id=?", (json.dumps(workspace), subagent_id))
    return result


def set_project_paused(conn, subagent_id, paused, archive):
    if archive:
        stage = conn.execute(
            """SELECT subagent_id, json_extract(workspace_json,'$.goal_state.status') AS status
               FROM delegated_projects WHERE archived=0
                 AND json_extract(workspace_json,'$.goal_state.executor_id')=?
                 AND json_extract(workspace_json,'$.goal_state.status') NOT IN ('done','cancelled')
               ORDER BY rowid LIMIT 1""",
            (subagent_id,),
        ).fetchone()
        if stage:
            raise ValueError(f"Cannot archive the executor while Child Goal {stage[0]} is {stage[1]}. "
                             "Complete or cancel the stage first.")
    conn.execute("UPDATE delegated_projects SET paused=?,archived=MAX(archived,?),updated_at=? WHERE subagent_id=?",
                 (int(paused), int(archive), time.time(), subagent_id))
    if archive:
        conn.execute("UPDATE delegated_project_messages SET state='cancelled',completed_at=?,published=1 WHERE subagent_id=? AND state='pending'", (time.time(), subagent_id))


def cancel_goal_inputs(conn, project_id):
    conn.execute(
        """UPDATE delegated_project_messages SET state='cancelled',completed_at=?,published=1
           WHERE state='pending' AND json_extract(origin_json, '$.goal_project_id')=?
             AND (COALESCE(json_extract(options_json, '$.agent_mail_wake'),0)=0
                  OR json_extract(options_json, '$.agent_mail_id') IS NOT NULL)""",
        (time.time(), project_id),
    )


def project_input_goal_id(message):
    options = json.loads(message['options_json'])
    if options.get('agent_mail_wake') and not options.get('agent_mail_id'):
        return None  # Legacy mailbox wakes copied the previous dispatch's route.
    return json.loads(message['origin_json']).get('goal_project_id')


def mail_wake_satisfied(conn, message):
    """A mailbox request is obsolete if its receipt was handled by another turn."""
    options = json.loads(message['options_json'])
    if options.get("child_goal_carrier"):
        # A descendant can have committed its input while the coordinator still
        # owes reconciliation of that turn's result after an owner restart.
        return False
    mail_id = options.get('agent_mail_id')
    return bool(mail_id and conn.execute(
        """SELECT 1 FROM delegation_mail mail WHERE mail.message_id=? AND mail.delivered_at IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.platform_message_id=? AND m.role='user'
               AND m.display_kind='agent_message' AND json_extract(
                 CASE WHEN json_valid(m.display_metadata) THEN m.display_metadata END,
                 '$.agent_mail_delivery_id')=mail.delivery_id AND EXISTS (SELECT 1 FROM json_each(
                 CASE WHEN json_valid(m.display_metadata) THEN m.display_metadata END, '$.agent_message_ids')
                 WHERE value=mail.message_id))""",
        (mail_id, message['message_id']),
    ).fetchone())


def _goal_input_row(conn, goal_id):
    return conn.execute(
        "SELECT json_extract(workspace_json, '$.goal_state.status'), archived, paused, "
        "json_extract(workspace_json, '$.goal_state.parent_goal_id') FROM delegated_projects WHERE subagent_id=?",
        (goal_id,),
    ).fetchone()


def _goal_input_state(conn, goal_id, *, child_parent=False):
    if not goal_id:
        return "open"
    row = _goal_input_row(conn, goal_id)
    if row is None or row[1] or row[0] in {None, "closing", "done", "cancelled", "inactive"}:
        return "closed"
    if row[3]:
        if child_parent:
            return "closed"
        ancestor = _goal_input_state(conn, row[3], child_parent=True)
        if ancestor != "open":
            return ancestor
    blocked = row[0] == "blocked" and (row[3] or child_parent)
    return "paused" if row[2] or blocked else "open"


def project_input_state(conn, message):
    options = json.loads(message["options_json"])
    state = _goal_input_state(conn, options.get("child_goal_carrier") or project_input_goal_id(message))
    if state != "open" or not json.loads(message["origin_json"]).get("child_goal_event"):
        return state
    receipt = conn.execute("SELECT origin_json,delivered_at FROM delegation_mail WHERE message_id=? AND kind='child_goal'",
                           (options.get("agent_mail_id"),)).fetchone()
    if not receipt:
        return "closed"
    if receipt[1] is not None:
        # The recipient already accepted this occurrence. The saved turn can still reconcile it.
        return "open"
    from tools.delegate_child_goals import child_event_state

    return child_event_state(conn, receipt[0])


def goal_has_recoverable_input(conn, project_id):
    if conn.execute(
        """SELECT 1 FROM delegation_mail WHERE delivered_at IS NULL
           AND json_extract(origin_json, '$.message_goal_project_id')=? LIMIT 1""",
        (project_id,),
    ).fetchone():
        return True
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT m.message_id,m.options_json,m.state,m.owner_pid,m.owner_started_at FROM delegated_project_messages m
           JOIN delegated_projects p USING(subagent_id)
           WHERE p.paused=0 AND p.archived=0 AND m.state IN ('pending','running')
             AND json_extract(m.origin_json, '$.goal_project_id')=?
             AND (COALESCE(json_extract(m.options_json, '$.agent_mail_wake'),0)=0
                  OR json_extract(m.options_json, '$.agent_mail_id') IS NOT NULL)""",
        (project_id,),
    ).fetchall()
    alive = _owner_liveness()
    return any((row['state'] == 'pending' or (alive is not None and not alive(row['owner_pid'], row['owner_started_at'])))
               and not mail_wake_satisfied(conn, row) for row in rows)


class ProjectStore:
    def __init__(self, parent):
        from hermes_constants import get_hermes_home
        db = getattr(parent, "_session_db", None)
        if db is None:
            raise ValueError("Persistent delegation requires the parent's SessionDB")
        from pathlib import Path
        self.db_path = Path(db.db_path)
        if self.db_path.resolve() != (get_hermes_home() / "state.db").resolve():
            raise ValueError("Project scope must match the owning profile's state.db")
        self.parent_id = str(parent.session_id)
        self._display_progress = getattr(parent, 'tool_progress_callback', None)
        from hermes_state_registry import acquire
        self.db = acquire(self.db_path)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        from hermes_state_registry import release_or_close
        release_or_close(self.db)

    def _db_call(self, method, *args, **kwargs):
        return getattr(self.db, method)(*args, **kwargs)

    def resolve(self, sid):
        return self._db_call("resolve_resume_session_id", sid) or sid

    def history(self, sid):
        return self._db_call("get_messages_as_conversation", sid, repair_alternation=True)

    def session_exists(self, sid):
        return self._db_call("get_session", sid) is not None

    def _owned(self, row):
        if row is None:
            raise ValueError("Unknown project subagent")
        owner = row["parent_session_id"]
        if self.resolve(owner) != self.resolve(self.parent_id):
            raise ValueError("This project belongs to another conversation")
        return dict(row)

    def get(self, subagent_id):
        with _transaction() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM delegated_projects WHERE subagent_id=?", (subagent_id,)).fetchone()
        return self._owned(row)

    def projects(self, query=""):
        # Never take SessionDB's lock while holding SQLite's writer lock: live mail can
        # arrive while a sibling is flushing its native transcript on the shared handle.
        with _transaction() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM delegated_projects ORDER BY updated_at DESC").fetchall()
            snapshot = []
            for row in rows:
                last = conn.execute("SELECT state,result_json,message_id FROM delegated_project_messages WHERE subagent_id=? ORDER BY accepted_at DESC LIMIT 1", (row["subagent_id"],)).fetchone()
                live = conn.execute("SELECT state FROM delegated_project_messages WHERE subagent_id=? AND state IN ('pending','running') ORDER BY accepted_at LIMIT 1", (row["subagent_id"],)).fetchone()
                snapshot.append((row, last, live))
        result = []
        for row, last, live in snapshot:
            try:
                project = self._owned(row)
            except ValueError:
                continue
            if query:
                words = set(re.findall(r"\w+", query.casefold()))
                text = (project["project_key"] + " " + project["goal"] + " " + (project["context"] or "")).casefold()
                project["match_score"] = len(words & set(re.findall(r"\w+", text))) + 5 * (query.casefold() in text)
                if not project["match_score"]:
                    continue
            project.update(status="archived" if project["archived"] else ("paused" if project["paused"] else (live[0] if live else "idle")))
            if last:
                project.update(last_message_id=last["message_id"], last_status=last["state"],
                               last_summary=(json.loads(last["result_json"] or "{}").get("summary") or "")[:2000])
            result.append(project)
        return sorted(result, key=lambda p: p.get("match_score", 0), reverse=True)

    def create(self, key, goal, context, cwd=None, *, workspace=None):
        if not isinstance(key, str) or not key.strip():
            raise ValueError("project must be a nonempty stable project/task key")
        key = key.strip()
        # Reuse identities across a native compression continuation of the parent.
        existing = next((p for p in self.projects() if p["project_key"] == key), None)
        if existing:
            if existing["archived"]:
                raise ValueError("Project is archived; choose another project key")
            return existing
        now = time.time()
        identity = "project-" + uuid.uuid4().hex
        with _transaction() as conn:
            goal_state = (workspace or {}).get("goal_state", {})
            if goal_state.get("parent_goal_id"):
                from tools.delegate_child_goals import open_parent

                if not open_parent(conn, goal_state):
                    raise ValueError("The parent Goal cannot accept a child")
            conn.execute("INSERT OR IGNORE INTO delegated_projects(subagent_id,parent_session_id,project_key,session_id,goal,context,created_at,updated_at,workspace_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (identity, self.parent_id, key, identity, goal, context, now, now,
                          json.dumps({"cwd": cwd, **(workspace or {})})))
            subagent_id = conn.execute("SELECT subagent_id FROM delegated_projects WHERE parent_session_id=? AND project_key=?", (self.parent_id, key)).fetchone()[0]
        return self.get(subagent_id)

    def enqueue(self, project, content, origin, event_key=None, options=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("A nonempty project message is required")
        message_id = "project-msg-" + uuid.uuid4().hex
        with _transaction() as conn:
            saved = conn.execute("SELECT archived FROM delegated_projects WHERE subagent_id=?",
                                 (project["subagent_id"],)).fetchone()
            if not saved:
                raise ValueError("Unknown project subagent")
            if saved[0]:
                raise ValueError("Project is archived")
            goal_id = origin.get("goal_project_id")
            if event_key is not None and goal_id:
                old = conn.execute(
                    "SELECT message_id FROM delegated_project_messages WHERE subagent_id=? AND event_key=? "
                    "AND json_extract(origin_json,'$.goal_project_id')=?",
                    (project["subagent_id"], event_key, goal_id),
                ).fetchone()
                if old is not None:
                    return old[0]
                event_key = f"goal:{goal_id}:{event_key}"
                old = conn.execute("SELECT message_id FROM delegated_project_messages WHERE subagent_id=? AND event_key=?",
                                   (project["subagent_id"], event_key)).fetchone()
                if old is not None:
                    return old[0]
            if goal_id:
                from tools.delegate_child_goals import bind_executor

                bind_executor(conn, goal_id, project)
            conn.execute("INSERT OR IGNORE INTO delegated_project_messages(message_id,subagent_id,event_key,content,origin_json,accepted_at,options_json) VALUES (?,?,?,?,?,?,?)",
                         (message_id, project["subagent_id"], event_key, content, json.dumps(origin), time.time(), json.dumps(options or {})))
            if event_key is not None:
                message_id = conn.execute("SELECT message_id FROM delegated_project_messages WHERE subagent_id=? AND event_key=?", (project["subagent_id"], event_key)).fetchone()[0]
        return message_id

    def claim(self, project):
        from gateway.status import get_process_start_time
        alive = _owner_liveness()
        with _transaction() as conn:
            state = conn.execute("SELECT archived,paused FROM delegated_projects WHERE subagent_id=?", (project["subagent_id"],)).fetchone()
            if not state or state[0] or state[1]:
                return None
            conn.row_factory = sqlite3.Row
            # BEGIN IMMEDIATE is owned by sqlite_util.transaction; one child turn per project.
            rows = conn.execute("SELECT * FROM delegated_project_messages WHERE subagent_id=? AND state IN ('pending','running') ORDER BY rowid", (project["subagent_id"],)).fetchall()
            for entry in rows:
                row = dict(entry)
                if row["state"] == "running" and (alive is None or alive(row["owner_pid"], row["owner_started_at"])):
                    return None
                goal_state = project_input_state(conn, row)
                if goal_state == "paused":
                    continue
                if goal_state == "closed" or mail_wake_satisfied(conn, row):
                    conn.execute("UPDATE delegated_project_messages SET state='cancelled',completed_at=?,published=1,"
                                 "owner_pid=NULL,claim_token=NULL WHERE message_id=?", (time.time(), row["message_id"]))
                    continue
                token = uuid.uuid4().hex
                conn.execute("UPDATE delegated_project_messages SET state='running',owner_pid=?,owner_started_at=?,claim_token=? WHERE message_id=?",
                             (os.getpid(), get_process_start_time(os.getpid()), token, row["message_id"]))
                row.update(claim_token=token, recovering=row["state"] == "running")
                return row
            return None

    def settle(self, project, message, result):
        from tools.delegate_child_goals import record_orchestration_result, record_worker_evidence

        canonical = result.get("status") == "completed" and self.committed_result(project, message) is not None
        with _transaction() as conn:
            goal_id = project_input_goal_id(message)
            if goal_id and conn.execute("SELECT 1 FROM delegated_project_messages WHERE message_id=? AND claim_token=?",
                                        (message["message_id"], message["claim_token"])).fetchone():
                record_worker_evidence(conn, project, message, result, goal_id)
                record_orchestration_result(conn, project, message, result, goal_id)
            changed = conn.execute("UPDATE delegated_project_messages SET state=?,result_json=?,completed_at=?,owner_pid=NULL,claim_token=NULL,published=?,content=CASE WHEN ? THEN '' ELSE content END,options_json=CASE WHEN ? THEN '{}' ELSE options_json END WHERE message_id=? AND claim_token=?",
                (result.get("status", "failed"), json.dumps(result, ensure_ascii=False), time.time(),
                 int(not json.loads(message["origin_json"]).get("wake")), canonical, canonical, message["message_id"], message["claim_token"])).rowcount
            if changed:
                conn.execute("UPDATE delegated_projects SET session_id=?,updated_at=? WHERE subagent_id=?",
                             (result.get("session_id") or project["session_id"], time.time(), project["subagent_id"]))
        if changed and self._display_progress:
            from tools.delegate_tool_progress import _safe_progress
            _safe_progress(self._display_progress, 'subagent.changed')
        return bool(changed)

    def release(self, message):
        with _transaction() as conn:
            conn.execute("UPDATE delegated_project_messages SET state='pending',owner_pid=NULL,claim_token=NULL WHERE message_id=? AND claim_token=?", (message["message_id"], message["claim_token"]))

    def committed_result(self, project, message):
        # The native loop persists assistant text before returning to the delegation worker.
        # A process death in that gap must deliver the answer, never execute the task again.
        sid = self.resolve(project["session_id"])
        with _transaction() as conn:
            if mail_wake_satisfied(conn, message):
                return None
            start = conn.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND platform_message_id=? AND role='user'", (sid, message["message_id"])).fetchone()[0]
            if start is None:
                return None
            tail = conn.execute("SELECT role,content,tool_calls,finish_reason FROM messages WHERE session_id=? AND id>? ORDER BY id DESC LIMIT 1", (sid, start)).fetchone()
            if tail and tail[0] == "assistant" and not tail[2] and tail[3] in ("stop", "end_turn"):
                return {"status": "completed", "summary": tail[1], "session_id": sid,
                        "subagent_id": project["subagent_id"], "recovered": True}
        return None

    def receipts(self, subagent_id):
        with _transaction() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM delegated_project_messages WHERE subagent_id=? AND state NOT IN ('pending','running') AND published=0", (subagent_id,))]

    def pause(self, subagent_id, paused=True, archive=False):
        project = self.get(subagent_id)
        if archive:
            from tools.delegate_wait_timer import archive_project

            return archive_project(self, project, paused)
        with _transaction() as conn:
            set_project_paused(conn, subagent_id, paused, False)

    def save_workspace(self, subagent_id, info):
        self.mutate_workspace(subagent_id, lambda workspace, _conn: workspace.update(worktree=info), active_only=False)

    def read_workspace(self, subagent_id):
        with self.db._read_ctx() as conn:
            row = conn.execute("SELECT * FROM delegated_projects WHERE subagent_id=?", (subagent_id,)).fetchone()
        project = self._owned(row)
        if project["archived"] or project["paused"]:
            raise ValueError("Project is paused or archived")
        return json.loads(project["workspace_json"])

    def mutate_workspace(self, subagent_id, change, *, active_only=True):
        owner = self.get(subagent_id)["parent_session_id"]
        with _transaction() as conn:
            row = conn.execute("SELECT workspace_json, archived, paused FROM delegated_projects WHERE subagent_id=? AND parent_session_id=?",
                               (subagent_id, owner)).fetchone()
            if row is None or (active_only and (row[1] or row[2])):
                raise ValueError("Unknown project subagent")
            # Workspace metadata has multiple writers. Merge against the row protected by this transaction.
            return write_workspace(conn, subagent_id, json.loads(row[0]), change)

    def instruction(self, subagent_id, proposed):
        # Native ephemeral child instructions are appended after the cached core
        # prompt, so persist them separately to keep reuse byte-stable as well.
        with _transaction() as conn:
            conn.execute("UPDATE delegated_projects SET instruction=COALESCE(instruction,?) WHERE subagent_id=?", (proposed, subagent_id))
            return conn.execute("SELECT instruction FROM delegated_projects WHERE subagent_id=?", (subagent_id,)).fetchone()[0]

    def message(self, message_id):
        with _transaction() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM delegated_project_messages WHERE message_id=?", (message_id,)).fetchone()
            return dict(row) if row else None
