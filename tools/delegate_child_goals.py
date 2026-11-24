"""Ownership and delivery for Goal stages assigned by a persistent orchestrator."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace


def orchestrator_turn(parent, store, project, message, child):
    from hermes_runtime.adapters.goal_turn import GoalSegment
    from tools.delegate_external_wait import _owns_main
    from tools.delegate_project_goal import active_goal, read_goal
    from tools.delegate_project_store import project_input_goal_id

    parent_goal_id = project_input_goal_id(message)
    if not parent_goal_id or getattr(child, "_delegate_role", None) != "orchestrator" or not _owns_main(parent):
        return None
    try:
        goal = read_goal(store, parent_goal_id)
    except ValueError:
        return None
    if not active_goal(goal) or goal["status"] == "blocked" or goal.get("parent_goal_id"):
        return None
    segment = GoalSegment(message["message_id"], True, parent_goal_id=parent_goal_id,
                          orchestrator_id=project["subagent_id"])
    event = json.loads(message["origin_json"]).get("child_goal_event")
    if event and event["parent_goal_id"] == parent_goal_id:
        segment.project_ids.add(event["project_id"])
        if event["source"] == "goal":
            segment.wake_ids[event["project_id"]] = event["event_id"]
    return segment


def open_children(conn, parent_goal_id):
    return conn.execute(
        """SELECT subagent_id, parent_session_id FROM delegated_projects
           WHERE json_extract(workspace_json, '$.goal_state.parent_goal_id')=?
             AND archived=0 AND json_extract(workspace_json, '$.goal_state.status') NOT IN
                 ('done','cancelled','inactive')""",
        (parent_goal_id,),
    ).fetchall()


def _project(conn, project_id):
    row = conn.execute(
        "SELECT subagent_id,parent_session_id,session_id,workspace_json,archived,paused "
        "FROM delegated_projects WHERE subagent_id=?",
        (project_id,),
    ).fetchone()
    return dict(zip(("id", "owner", "session", "workspace", "archived", "paused"), row, strict=True)) if row else None


def _same_session(conn, first, second):
    from hermes_state_runtime import _canonical_chain

    return first == second or first in _canonical_chain(conn, second) or second in _canonical_chain(conn, first)


def _orchestrator(conn, project, goal):
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_state_runtime import _canonical_chain

    parent = _project(conn, goal["parent_goal_id"])
    coordinator = _project(conn, goal["orchestrator_id"])
    home = _canonical_chain(conn, HOME_SESSION_ID)
    if (not parent or not coordinator or parent["owner"] not in home or coordinator["owner"] not in home
            or not _same_session(conn, project["owner"], coordinator["session"])):
        raise ValueError("Child Goal does not belong to the orchestrator of a managed assignment")
    return coordinator


def validate_wait_owner(conn, project_id, workspace):
    goal = workspace.get("goal_state", {})
    if not goal.get("parent_goal_id"):
        return False
    _orchestrator(conn, _project(conn, project_id), goal)
    return True


def open_parent(conn, goal):
    from tools.delegate_project_store import _goal_input_state

    return _goal_input_state(conn, goal["parent_goal_id"], child_parent=True) == "open"


def bind_executor(conn, goal_id, project):
    from tools.delegate_project_store import _goal_input_state, write_workspace

    row = _project(conn, goal_id)
    if not row:
        return
    workspace = json.loads(row["workspace"])
    goal = workspace.get("goal_state", {})
    if not goal.get("parent_goal_id"):
        return
    if (not _same_session(conn, row["owner"], project["parent_session_id"])
            or _goal_input_state(conn, goal_id) != "open"):
        raise ValueError("Child Goal is outside this executor's active assignment")
    executor = goal.get("executor_id")
    if executor and executor != project["subagent_id"]:
        raise ValueError("Child Goal already has an executor")
    if not executor:
        goal["executor_id"] = project["subagent_id"]
        write_workspace(conn, goal_id, workspace, lambda _workspace, _conn: None)


def close_children(store, parent_goal_id):
    from tools.delegate_project_goal import close_goal, read_goal
    from tools.delegate_project_store import ProjectStore, _transaction

    with _transaction() as conn:
        children = open_children(conn, parent_goal_id)
    for project_id, owner in children:
        with ProjectStore(SimpleNamespace(session_id=owner, _session_db=store.db)) as child_store:
            goal = read_goal(child_store, project_id)
            if goal["status"] == "closing":
                close_goal(child_store, project_id, goal["outcome"], goal["final_evidence"])
            else:
                close_goal(child_store, project_id, "cancelled")


def recovery_owner(conn, project_id, goal):
    record = _project(conn, project_id)
    if record is None:
        return None
    coordinator = _orchestrator(conn, record, goal)
    if goal["status"] == "closing":
        return record["owner"]
    if not open_parent(conn, goal):
        return None
    busy = conn.execute(
        """SELECT 1 FROM delegated_project_messages WHERE subagent_id=? AND state IN ('pending','running')
           AND json_extract(origin_json, '$.goal_project_id')=? LIMIT 1""",
        (coordinator["id"], goal["parent_goal_id"]),
    ).fetchone()
    return None if busy else record["owner"]


def child_scope_for_mail(conn, goal_id, project):
    row = _project(conn, goal_id)
    if not row:
        return goal_id
    goal = json.loads(row["workspace"]).get("goal_state", {})
    if goal.get("orchestrator_id") == project["subagent_id"]:
        return goal["parent_goal_id"]
    return goal_id


def validate_child_mail_target(conn, goal_id, target):
    row = _project(conn, goal_id)
    if not row:
        return
    goal = json.loads(row["workspace"]).get("goal_state", {})
    if not goal.get("parent_goal_id"):
        return
    coordinator = _orchestrator(conn, row, goal)
    worker = _project(conn, goal.get("executor_id"))
    if not (_same_session(conn, target, coordinator["session"])
            or worker and _same_session(conn, target, worker["session"])):
        raise ValueError("Address the executor or orchestrator of the selected Child Goal")


def child_event_state(conn, origin_json):
    from tools.delegate_project_store import _goal_input_state

    event = json.loads(origin_json).get("child_goal_event")
    if not isinstance(event, dict):
        return "closed"
    record = _project(conn, event["project_id"])
    if not record or record["archived"]:
        return "closed"
    workspace = json.loads(record["workspace"])
    goal = workspace.get("goal_state", {})
    if (goal.get("parent_goal_id"), goal.get("orchestrator_id")) != (event["parent_goal_id"], event["orchestrator_id"]):
        return "closed"
    if event["source"] == "result":
        if goal["status"] != event["status"] or _child_result_id(event["project_id"], goal) != event["event_id"]:
            return "closed"
        parent_state = _goal_input_state(conn, event["parent_goal_id"], child_parent=True)
        return "paused" if parent_state == "open" and record["paused"] else parent_state
    if event["source"] == "goal":
        current = goal.get("wake_id") == event["event_id"] and goal["status"] == "ready"
    elif event["source"] == "cron":
        wait = workspace.get("external_wait") or {}
        current = (wait.get("state") == "BOUND" and wait.get("wait_id") == event["wait_id"]
                   and wait.get("job_id") == event["job_id"] and datetime.fromisoformat(wait["deadline"]) > datetime.now(UTC))
    else:
        current = False
    return _goal_input_state(conn, event["project_id"]) if current else "closed"


def consumed_child_wake(store, wake_id):
    return bool(store.db._read_one(
        "SELECT 1 FROM delegation_mail WHERE event_key=? AND delivered_at IS NOT NULL", ("child-goal:" + wake_id,),
    ))


def publish_child_event(project_id, event_id, prompt, source, **metadata):
    """Durably address the next turn to the assigned worker or its orchestrator."""
    from hermes_runtime.adapters.state import current_home_session_id
    from hermes_state import SessionDB
    from tools.delegate_mail import publish_pending_mail
    from tools.delegate_mail_store import MailStore
    from tools.delegate_project_store import _transaction

    with _transaction() as conn:
        record = _project(conn, project_id)
        if record is None:
            raise ValueError("Child Goal no longer exists")
        goal = json.loads(record["workspace"])["goal_state"]
        coordinator = _orchestrator(conn, record, goal)
        worker = _project(conn, goal.get("executor_id"))
        if source == "cron" and worker is None:
            raise ValueError("Child Goal timer has no saved executor")
    parent_goal_id = goal["parent_goal_id"]
    to_worker = worker is not None and source != "result"
    if to_worker:
        sender, target, scope_id = coordinator["session"], worker["id"], project_id
    else:
        sender = worker["session"] if worker else current_home_session_id()
        target, scope_id = coordinator["id"], parent_goal_id
    event = dict(source=source, event_id=event_id, project_id=project_id, parent_goal_id=parent_goal_id,
                 orchestrator_id=coordinator["id"], scope_id=scope_id, **metadata)
    with SessionDB() as db, MailStore(SimpleNamespace(session_id=sender, _session_db=db)) as mail:
        receipt = mail.send(target, prompt, "child-goal:" + event_id, kind="child_goal", child_event=event)
    publish_pending_mail()
    return receipt


def _child_result_id(project_id, goal):
    status = goal["status"]
    suffix = status if status == "done" else f"{status}:{goal['wake_sequence']}:{goal['revision']}"
    return f"{project_id}:{suffix}"


def publish_child_result(project_id, goal):
    status = goal["status"]
    prompt = (f"Child Goal {project_id} is {status}. Use project_goal select with this ID to read its "
              "saved evidence or blocker and decide the next stage of the parent assignment.")
    return publish_child_event(project_id, _child_result_id(project_id, goal), prompt, "result", status=status)


def record_worker_evidence(conn, project, message, result, goal_id):
    from tools.delegate_project_goal import add_evidence, active_goal
    from tools.delegate_project_store import write_workspace

    row = _project(conn, goal_id)
    if not row:
        return
    workspace = json.loads(row["workspace"])
    goal = workspace.get("goal_state") or {}
    if goal.get("executor_id") != project["subagent_id"]:
        return
    result["child_goal_id"] = goal_id
    if not active_goal(goal):
        return
    report = json.dumps({key: result.get(key) for key in ("status", "summary", "error", "exit_reason")},
                        ensure_ascii=False)[:6000]
    if report == goal.get("last_worker_report"):
        return
    if add_evidence(goal, message["message_id"], "worker report", report):
        goal["last_worker_report"] = report
        write_workspace(conn, goal_id, workspace, lambda _workspace, _conn: None)


def unfinished_orchestration(conn, project, message, goal_id):
    if not goal_id:
        return False
    rows = conn.execute(
        """SELECT json_extract(workspace_json, '$.goal_state.status'),
                  json_extract(workspace_json, '$.goal_state.escalation_input_id')
           FROM delegated_projects WHERE json_extract(workspace_json, '$.goal_state.parent_goal_id')=?
             AND json_extract(workspace_json, '$.goal_state.orchestrator_id')=? AND archived=0""",
        (goal_id, project["subagent_id"]),
    ).fetchall()
    if not rows or any(status == "blocked" and escalation == message["message_id"] for status, escalation in rows):
        return False
    if any(status not in {"done", "cancelled", "inactive"} for status, _ in rows):
        return True
    rows = conn.execute(
        """SELECT origin_json FROM delegation_mail WHERE delivered_at IS NULL AND kind='child_goal'
             AND json_extract(origin_json, '$.child_goal_event.orchestrator_id')=?
             AND json_extract(origin_json, '$.child_goal_event.parent_goal_id')=?
             AND json_extract(origin_json, '$.child_goal_event.source')='result'""",
        (project["subagent_id"], goal_id),
    ).fetchall()
    return any(child_event_state(conn, row[0]) != "closed" for row in rows)
