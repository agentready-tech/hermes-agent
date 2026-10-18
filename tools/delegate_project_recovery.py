"""Wake owning conversations through Hermes' existing durable completion delivery."""

import hashlib
import json
import time

from tools.async_delegation import _owner_liveness
from tools.delegate_project_store import _transaction


def publish_recovery_wakeups():
    from tools.delegate_mail import publish_pending_mail
    mail_count = publish_pending_mail()
    alive = _owner_liveness()
    if alive is None:
        return mail_count
    with _transaction() as conn:
        rows = conn.execute("SELECT p.parent_session_id,p.project_key,p.subagent_id,m.message_id,m.origin_json,"
            "COALESCE(m.owner_pid,a.owner_pid),CASE WHEN m.owner_pid IS NOT NULL THEN m.owner_started_at ELSE a.owner_started_at END "
            "FROM delegated_project_messages m JOIN delegated_projects p USING(subagent_id) "
            "LEFT JOIN async_delegations a ON a.delegation_id=m.message_id "
            "WHERE (m.state IN ('pending','running') OR m.published=0) AND p.archived=0 AND p.paused=0").fetchall()
    count = mail_count
    live_projects = {sid for _, _, sid, _, _, pid, started in rows if pid and alive(pid, started)}
    for parent, key, sid, mid, raw_origin, pid, started in rows:
        if sid in live_projects:
            continue
        origin = json.loads(raw_origin)
        if not origin.get("wake"):
            continue
        identity = "project-recovery-" + hashlib.sha256(f"{mid}:{pid}:{started}".encode()).hexdigest()
        summary = (f"Project {key}, persistent subagent {sid}, has accepted work or a saved result awaiting delivery ({mid}). "
                   "Its old execution process is gone. Resume the saved project queue and inspect the existing "
                   "history/results before repeating external actions. Do not create another project child.")
        if publish_wakeup(identity, parent, origin, summary):
            count += 1
    return count


def publish_wakeup(identity, parent, origin, summary, *, agent_message=False):
    # One deterministic recovery notice per abandoned owner/input, including across
    # the managed serve and messaging gateway processes sharing this profile.
    with _transaction() as conn:
        if conn.execute("SELECT 1 FROM async_delegations WHERE delegation_id=?", (identity,)).fetchone():
            return False
        now = time.time()
        event = {"type": "async_delegation", "delegation_id": identity, "parent_session_id": parent,
            "durable_recovery": True, "agent_message": agent_message,
            "origin_session_id": parent, "status": "completed", "summary": summary,
            "goal": "Restore accepted work after restart", "dispatched_at": now, "completed_at": now,
            **{k: v for k, v in origin.items() if k != "wake"}}
        event["session_key"] = event.get("session_key") or parent
        # Persist both halves on the same admitted transaction, rather than holding
        # this lock while calling the public completion writers on another connection.
        conn.execute("INSERT INTO async_delegations(delegation_id,origin_session,origin_ui_session_id,parent_session_id,state,dispatched_at,completed_at,updated_at,event_json,result_json,delivery_state,origin_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, event.get("session_key", ""), event.get("origin_ui_session_id", ""), parent, "completed", now,
             now, now, json.dumps(event), json.dumps({"summary": summary}), "pending", parent))
    return True
