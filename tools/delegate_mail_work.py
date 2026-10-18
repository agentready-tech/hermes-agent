"""Use the existing per-project task queue to resume recipients with pending mail."""

import json
from types import SimpleNamespace

from tools.delegate_mail_store import MailStore
from tools.delegate_project_store import _transaction


def _below(store, target, ancestor):
    target, ancestor, seen = store.tip(target), store.tip(ancestor), set()
    while target and target not in seen:
        node = store.nodes.get(target, {})
        if node.get("paused") or node.get("archived"):
            return False
        if target == ancestor:
            return True
        seen.add(target)
        target = store.parent(target)
    return False


def ensure_mail_input(parent, projects, project):
    if project["archived"] or project["paused"]:
        return
    with MailStore(parent) as mail:
        mail.acknowledge_recorded()
        with _transaction() as conn:
            rows = conn.execute("SELECT message_id,target_session_id FROM delegation_mail WHERE delivered_at IS NULL ORDER BY rowid").fetchall()
            active = conn.execute("SELECT 1 FROM delegated_project_messages WHERE subagent_id=? AND state IN ('pending','running')",
                                  (project["subagent_id"],)).fetchone()
            previous = conn.execute("SELECT origin_json FROM delegated_project_messages WHERE subagent_id=? ORDER BY rowid DESC LIMIT 1",
                                    (project["subagent_id"],)).fetchone()
        if active:
            return
        first = next((identity for identity, target in rows if _below(mail, target, project["session_id"])), None)
        if first is None:
            return
        from tools.delegate_projects import _origin
        origin = json.loads(previous[0]) if previous else _origin()
        if getattr(parent, "_delegate_depth", 0):
            origin = {**origin, "wake": False}
        if not origin.get("wake"):
            origin["mail_results"] = True
        projects.enqueue(project, "Continue your existing task and handle pending agent messages. "
                         "Their contents will enter your conversation at a safe boundary.", origin, "mail-wake:" + first, {"agent_mail_wake": True})


def result_mail(parent, project, message, result):
    if not json.loads(message["origin_json"]).get("mail_results"):
        return
    actor = SimpleNamespace(session_id=result.get("session_id") or project["session_id"], _session_db=parent._session_db)
    with MailStore(actor) as mail:
        mail.send("parent", f"Your child {project['subagent_id']} finished input {message['message_id']} "
                  f"with status {result.get('status', 'failed')}. Use delegate_task(action='read', "
                  f"subagent_id='{project['subagent_id']}') for its saved result and evidence.",
                  "result:" + message["message_id"], kind="result", wake=False)


def recover_result_mail(parent, store, project):
    with _transaction() as conn:
        rows = conn.execute("SELECT message_id,result_json FROM delegated_project_messages WHERE subagent_id=? "
            "AND state NOT IN ('pending','running') AND json_extract(origin_json,'$.mail_results')=1 "
            "AND NOT EXISTS(SELECT 1 FROM delegation_mail WHERE event_key='result:' || delegated_project_messages.message_id)",
            (project["subagent_id"],)).fetchall()
    for identity, raw in rows:
        if raw:
            result_mail(parent, project, store.message(identity), json.loads(raw))


def stage_mail_input(child, message, history):
    """The inbox wake's first user row carries the actual mail, never a blind request to repeat old work."""
    with MailStore(child) as mail:
        live_ids = {identity for row in history if (identity := row.get("message_id") or row.get("platform_message_id"))}
        row = mail.delivery(live_ids)
        if row is None:
            return message["content"], None
        metadata = {**row["display_metadata"], "agent_mail_delivery_id": message["message_id"]}
        with _transaction() as conn:
            conn.executemany("UPDATE delegation_mail SET delivery_id=? WHERE message_id=? AND delivered_at IS NULL",
                             [(message["message_id"], identity) for identity in metadata["agent_message_ids"]])
        return row["content"], metadata
