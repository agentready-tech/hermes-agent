"""Use the existing per-project task queue to resume recipients with pending mail."""

import json
from types import SimpleNamespace

from tools.async_delegation import _owner_liveness
from tools.delegate_mail_store import MailStore, pending_mail_rows
from tools.delegate_project_store import _transaction, mail_wake_satisfied, project_input_state


class MailboxDeferred(ValueError):
    """A claimed mailbox wake must return to the queue for reevaluation."""

    def __init__(self, *, pending_delivery=False):
        super().__init__("The addressed mail is unavailable for this turn")
        self.pending_delivery = pending_delivery


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


def ensure_mail_input(parent, projects, project, *, mail_id=None):
    if project["archived"] or project["paused"]:
        return
    with MailStore(parent) as mail:
        mail.acknowledge_recorded()
        with _transaction() as conn:
            rows = pending_mail_rows(conn, message_ids=(mail_id,) if mail_id else None)
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
        receipt_origin = json.loads(first["origin_json"])
        event = receipt_origin.get("child_goal_event") if first["kind"] == "child_goal" else None
        child_mail = event is not None or scope != first["goal_project_id"]
        nested_owner = getattr(parent, "_delegate_depth", 0)
        from tools.delegate_projects import _origin

        # A synchronous first dispatch can still have a later, explicitly routed continuation.
        mail_origin = getattr(parent, "_project_mail_origin", None)
        direct = mail_origin and not scope
        if direct:
            origin = {**mail_origin, "wake": False}
        elif child_mail and not nested_owner and receipt_origin.get("wake"):
            origin = receipt_origin.copy()
        else:
            origin = json.loads(previous[0]) if previous else _origin()
        origin.pop("goal_project_id", None)
        origin.pop("child_goal_event", None)
        origin.pop("child_goal_mail", None)
        origin.pop("message_goal_project_id", None)
        origin.pop("mail_results", None)
        if scope:
            origin['goal_project_id'] = scope
        options = {'agent_mail_wake': True, 'agent_mail_id': first['message_id']}
        if child_mail:
            origin["child_goal_mail"] = True
        if scope != first["goal_project_id"] and mail.tip(first["target_session_id"]) != mail.tip(project["session_id"]):
            options["child_goal_carrier"] = first["goal_project_id"]
        if event is not None:
            origin['child_goal_event'] = event
        if nested_owner:
            origin = {**origin, "wake": False}
        if not origin.get("wake") and not direct:
            origin["mail_results"] = True
        return projects.enqueue(project, "Continue your existing task and handle pending agent messages. "
                         "Their contents will enter your conversation at a safe boundary.", origin,
                         'mail-wake:' + first['message_id'], options)


def _blocks_mail(conn, row):
    if row['state'] == 'running':
        alive = _owner_liveness()
        if alive is None or alive(row['owner_pid'], row['owner_started_at']):
            return True
    return project_input_state(conn, row) == 'open' and not mail_wake_satisfied(conn, row)


def result_mail(parent, project, message, result):
    if (not json.loads(message["origin_json"]).get("mail_results")
            or result.get("child_goal_id") or result.get("goal_continuing")):
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
            "AND COALESCE(json_extract(result_json,'$.goal_continuing'),0)=0 "
            "AND NOT EXISTS(SELECT 1 FROM delegation_mail WHERE event_key='result:' || delegated_project_messages.message_id)",
            (project["subagent_id"],)).fetchall()
    for identity, raw in rows:
        if raw:
            result_mail(parent, project, store.message(identity), json.loads(raw))


def stage_mail_input(child, message, history):
    """The inbox wake's first user row carries the actual mail, never a blind request to repeat old work."""
    with MailStore(child) as mail:
        live_ids = {identity for row in history if (identity := row.get("message_id") or row.get("platform_message_id"))}
        options = json.loads(message["options_json"])
        first_id = options.get("agent_mail_id")
        row = mail.delivery(live_ids, first_id=first_id, request_id=message['message_id'])
        if row is None:
            if first_id:
                with _transaction() as conn:
                    if project_input_state(conn, message) != "open" or mail_wake_satisfied(conn, message):
                        raise MailboxDeferred
            if json.loads(message["origin_json"]).get("child_goal_event") and not options.get("child_goal_carrier"):
                raise MailboxDeferred(pending_delivery=True)
            return message["content"], None
        metadata = {**row['display_metadata'], 'agent_mail_delivery_id': row['message_id']}
        return row["content"], metadata
