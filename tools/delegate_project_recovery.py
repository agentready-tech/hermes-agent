"""Wake owning conversations through Hermes' existing durable completion delivery."""

import hashlib
import json
import sqlite3
import time

from tools.async_delegation import _owner_liveness
from tools.delegate_project_store import _transaction, mail_wake_satisfied, project_input_goal_id


def repair_legacy_completion_route(conn, event, payload):
    """Replay an old project's saved result through its unique persisted owner route.

    The old project producer stamped a push owner as a raw API id. Never infer
    an API route, borrow another profile's routing index, or execute the child.
    Normal completion ownership/admission checks still run after this repair.
    """
    from hermes_constants import get_hermes_home
    parent = event.get("parent_session_id")
    if (event.get("type") != "async_delegation" or not parent
            or event.get("session_key") != parent or event.get("origin_session_id") != parent):
        return event
    identity = event.get("delegation_id")
    row = conn.execute("SELECT p.parent_session_id,m.origin_json FROM delegated_project_messages m "
        "JOIN delegated_projects p USING(subagent_id) WHERE m.message_id=? "
        "AND m.state NOT IN ('pending','running')", (identity,)).fetchone()
    if row is None or row[0] != parent:
        return event
    origin = json.loads(row[1])
    if not origin.get("wake") or origin.get("session_key") or "origin_session_id" in origin:
        return event
    scope = str((get_hermes_home() / "sessions").resolve())
    routes = conn.execute("SELECT session_key,entry_json FROM gateway_routing WHERE scope=? "
        "AND json_extract(entry_json,'$.session_id')=?", (scope, parent)).fetchall()
    if len(routes) != 1:
        return event
    route, raw = routes[0]
    entry = json.loads(raw)
    platform = (entry.get("origin") or {}).get("platform")
    if entry.get("suspended") or not platform or platform == "api_server":
        return event
    repaired = {**event, "session_key": route, "origin_session_id": ""}
    changed = conn.execute("UPDATE async_delegations SET origin_session=?,origin_session_id='',event_json=? "
        "WHERE delegation_id=? AND event_json=? AND delivery_state='pending'",
        (route, json.dumps(repaired), identity, payload)).rowcount
    if not changed:
        return event
    conn.execute("UPDATE delegated_project_messages SET origin_json=? WHERE message_id=? AND origin_json=?",
        (json.dumps({**origin, "session_key": route, "origin_session_id": ""}), identity, row[1]))
    return repaired


def publish_recovery_wakeups():
    from tools.delegate_mail import publish_pending_mail
    mail_count = publish_pending_mail()
    alive = _owner_liveness()
    if alive is None:
        return mail_count
    with _transaction() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT p.parent_session_id,p.project_key,p.subagent_id,m.message_id,m.origin_json,m.options_json,"
            "COALESCE(m.owner_pid,a.owner_pid) AS pid,CASE WHEN m.owner_pid IS NOT NULL THEN m.owner_started_at ELSE a.owner_started_at END AS started,"
            "(COALESCE(g.paused,0) OR COALESCE(g.archived,0) OR COALESCE("
            "json_extract(g.workspace_json, '$.goal_state.status') IN ('closing','done','cancelled','inactive','blocked'),0)) AS goal_quiet "
            "FROM delegated_project_messages m JOIN delegated_projects p USING(subagent_id) "
            "LEFT JOIN async_delegations a ON a.delegation_id=m.message_id "
            "LEFT JOIN delegated_projects g ON g.subagent_id=json_extract(m.origin_json, '$.goal_project_id') "
            "AND (COALESCE(json_extract(m.options_json, '$.agent_mail_wake'),0)=0 "
            "     OR json_extract(m.options_json, '$.agent_mail_id') IS NOT NULL) "
            "WHERE (m.state IN ('pending','running') OR m.published=0) AND p.archived=0 AND p.paused=0").fetchall()
        live_projects = {row['subagent_id'] for row in rows if row['pid'] and alive(row['pid'], row['started'])}
        rows = [row for row in rows if not row['goal_quiet'] and not mail_wake_satisfied(conn, row)]
    count = mail_count
    for row in rows:
        sid, mid = row['subagent_id'], row['message_id']
        origin = json.loads(row['origin_json'])
        mail_root = origin.get('project_mail_root')
        if sid in live_projects or not (origin.get('wake') or mail_root):
            continue
        if not project_input_goal_id(row):
            origin.pop('goal_project_id', None)
        if json.loads(row['options_json']).get('agent_mail_id'):
            origin['mail_project_message_id'] = mid
        identity = 'project-recovery-' + hashlib.sha256(f"{mid}:{row['pid']}:{row['started']}".encode()).hexdigest()
        summary = (f"Project {row['project_key']}, persistent subagent {sid}, has accepted work or a saved result awaiting delivery ({mid}). "
                   "Its old execution process is gone. Resume the saved project queue and inspect the existing "
                   "history/results before repeating external actions. Do not create another project child.")
        if publish_wakeup(identity, mail_root or row['parent_session_id'], origin, summary):
            count += 1
    return count


def publish_wakeup(identity, parent, origin, summary, *, agent_message=False, goal="Restore accepted work after restart"):
    # One deterministic recovery notice per abandoned owner/input, including across
    # the managed serve and messaging gateway processes sharing this profile.
    with _transaction() as conn:
        if conn.execute("SELECT 1 FROM async_delegations WHERE delegation_id=?", (identity,)).fetchone():
            return False
        now = time.time()
        event = {"type": "async_delegation", "delegation_id": identity, "parent_session_id": parent,
            "durable_recovery": True, "agent_message": agent_message,
            "origin_session_id": origin.get("origin_session_id", ""), "status": "completed", "summary": summary,
            "goal": goal, "dispatched_at": now, "completed_at": now,
            **{k: v for k, v in origin.items() if k != "wake"}}
        event["session_key"] = event.get("session_key") or parent
        # Persist both halves on the same admitted transaction, rather than holding
        # this lock while calling the public completion writers on another connection.
        conn.execute("INSERT INTO async_delegations(delegation_id,origin_session,origin_ui_session_id,parent_session_id,state,dispatched_at,completed_at,updated_at,event_json,result_json,delivery_state,origin_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, event.get("session_key", ""), event.get("origin_ui_session_id", ""), parent, "completed", now,
             now, now, json.dumps(event), json.dumps({"summary": summary}), "pending", parent))
    return True
