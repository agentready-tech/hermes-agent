"""Use the existing per-project task queue to resume recipients with pending mail."""

import json
from types import SimpleNamespace

from tools.async_delegation import _owner_liveness
from tools.delegate_mail_store import MailStore, pending_mail_rows
from tools.delegate_project_store import _goal_input_state, _transaction, mail_wake_satisfied, project_input_goal_id


class MailboxDeferred(ValueError):
    """A claimed mailbox wake must return to the queue for reevaluation."""


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
            rows = pending_mail_rows(conn)
            active = conn.execute("SELECT * FROM delegated_project_messages WHERE subagent_id=? AND state IN ('pending','running')",
                                  (project["subagent_id"],)).fetchall()
            if any(_blocks_mail(conn, row) for row in active):
                return
            previous = conn.execute("SELECT origin_json FROM delegated_project_messages WHERE subagent_id=? ORDER BY rowid DESC LIMIT 1",
                                    (project["subagent_id"],)).fetchone()
        first = next((row for row in rows if _below(mail, row['target_session_id'], project['session_id'])), None)
        if first is None:
            return
        from tools.delegate_child_goals import child_scope_for_mail

        with _transaction() as conn:
            scope = child_scope_for_mail(conn, first['goal_project_id'], project)
        from tools.delegate_projects import _origin
        origin = json.loads(previous[0]) if previous else _origin()
        origin.pop("goal_project_id", None)
        origin.pop("child_goal_event", None)
        origin.pop("child_goal_mail", None)
        if scope:
            origin['goal_project_id'] = scope
        options = {'agent_mail_wake': True, 'agent_mail_id': first['message_id']}
        if scope != first['goal_project_id']:
            origin["child_goal_mail"] = True
            if mail.tip(first["target_session_id"]) != mail.tip(project["session_id"]):
                options["child_goal_carrier"] = first["goal_project_id"]
        if first['kind'] == 'child_goal':
            event = json.loads(first['origin_json'])['child_goal_event']
            origin['child_goal_event'] = event
            origin["child_goal_mail"] = True
        if getattr(parent, "_delegate_depth", 0):
            origin = {**origin, "wake": False}
        if not origin.get("wake"):
            origin["mail_results"] = True
        projects.enqueue(project, "Continue your existing task and handle pending agent messages. "
                         "Their contents will enter your conversation at a safe boundary.", origin,
                         'mail-wake:' + first['message_id'], options)


def _blocks_mail(conn, row):
    if row['state'] == 'running':
        alive = _owner_liveness()
        if alive is None or alive(row['owner_pid'], row['owner_started_at']):
            return True
    carrier = json.loads(row["options_json"]).get("child_goal_carrier")
    return _goal_input_state(conn, carrier or project_input_goal_id(row)) == 'open' and not mail_wake_satisfied(conn, row)


def result_mail(parent, project, message, result):
    if not json.loads(message["origin_json"]).get("mail_results") or result.get("child_goal_id"):
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
            "AND json_extract(result_json,'$.child_goal_id') IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM delegation_mail WHERE event_key='result:' || delegated_project_messages.message_id)",
            (project["subagent_id"],)).fetchall()
    for identity, raw in rows:
        if raw:
            result_mail(parent, project, store.message(identity), json.loads(raw))


def stage_mail_input(child, message, history):
    """The inbox wake's first user row carries the actual mail, never a blind request to repeat old work."""
    with MailStore(child) as mail:
        live_ids = {identity for row in history if (identity := row.get("message_id") or row.get("platform_message_id"))}
        first_id = json.loads(message['options_json']).get('agent_mail_id')
        row = mail.delivery(live_ids, first_id=first_id, request_id=message['message_id'])
        if row is None:
            if first_id:
                with _transaction() as conn:
                    if (_goal_input_state(conn, project_input_goal_id(message)) != 'open'
                            or mail_wake_satisfied(conn, message)):
                        raise MailboxDeferred
            return message["content"], None
        metadata = {**row['display_metadata'], 'agent_mail_delivery_id': row['message_id']}
        return row["content"], metadata
