"""Project-scoped Goal commands for the owning Bot Mode coordinator."""

import hashlib
import json
import sqlite3

from tools.delegate_project_store import ProjectStore
from tools.registry import tool_error

KEY = "goal_state"
TERMINAL = frozenset({"done", "cancelled", "closing", "inactive"})
FIELDS = {
    "open": {"goal", "verification", "constraints", "tracker_ref", "slot"},
    "list": set(),
    "select": {"project_id"},
    "checkpoint": {"project_id", "stage", "next_step", "evidence_tool_call_id", "evidence_tool_name",
                   "deliverable", "notify", "await_operation_id"},
    "block": {"project_id", "blocker", "unblock", "notify"},
    "pause": {"project_id"},
    "resume": {"project_id"},
    "cancel": {"project_id"},
}


def schema_fields():
    return {
        "verification": {"type": "string", "description": "Observable criteria for the final goal"},
        "constraints": {"type": "string", "description": "Boundaries agreed with the requester"},
        "tracker_ref": {"type": "string", "description": "Stable workspace and issue ID for an accepted tracker assignment"},
        "slot": {"type": "integer", "minimum": 1, "description": "Ordinal for different goals in one admitted request"},
        "stage": {"type": "string"},
        "next_step": {"type": "string", "description": "One concrete action the executor can take now. Leave empty when there is no action to take."},
        "evidence_tool_call_id": {"type": "string", "description": "ID of an actual result from a tool in this turn"},
        "evidence_tool_name": {"type": "string", "description": "Use the newest result from this tool in the current turn"},
        "await_operation_id": {"type": "string", "description": "Wait for a confirmed durable operation receipt"},
        "deliverable": {"type": "string", "description": "The actual requested text artifact, not a claim about external work"},
        "notify": {"type": "boolean", "description": "The checkpoint is a significant update worth delivering"},
        "blocker": {"type": "string"},
        "unblock": {"type": "string", "description": "The specific condition or decision required to continue"},
    }


def read_goal(store, project_id):
    project = store.get(project_id)
    goal = json.loads(project["workspace_json"]).get(KEY)
    if not isinstance(goal, dict):
        raise ValueError("No Goal is open for this project")
    if project["archived"] or project["paused"]:
        return {**goal, "status": "inactive"}
    return goal


def mutate_goal(store, project_id, change, *, retire_pending=False, when_paused=None):
    def apply(workspace, conn):
        current = workspace.get(KEY)
        if not isinstance(current, dict):
            raise ValueError("No Goal is open for this project")
        if when_paused:
            paused, archived = conn.execute(
                "SELECT paused, archived FROM delegated_projects WHERE subagent_id=?", (project_id,),
            ).fetchone()
            if archived or (paused and when_paused == "skip"):
                return {**current, "status": "inactive"}
        if when_paused == "skip" and current.get("parent_goal_id") and current["status"] != "closing":
            from tools.delegate_child_goals import open_parent

            if not open_parent(conn, current):
                return {**current, "status": "inactive"}
        updated = change(current)
        if retire_pending and updated["status"] == "closing":
            from tools.delegate_child_goals import open_children
            from tools.delegate_project_store import cancel_goal_inputs, set_project_paused

            if updated["outcome"] == "done" and open_children(conn, project_id):
                raise ValueError("Complete the child Goals before closing their parent")
            set_project_paused(conn, project_id, False, False)
            cancel_goal_inputs(conn, project_id)
        return updated
    return store.mutate_workspace(project_id, apply, active_only=when_paused is None)


def active_goal(goal):
    return goal["status"] not in TERMINAL


def owned_operation(store, identity):
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    operations = ToolOperationStore(store.db_path.resolve().parent)
    row = operations.get(identity)
    if not row or store.resolve(row["session_id"]) != store.resolve(store.parent_id):
        return None, None
    return operations, row


def operation_returns(store, identity):
    operations, row = owned_operation(store, identity)
    return bool(row and operations.continuation_state(identity, row["session_id"])["return_path"])


def add_evidence(goal, reference, kind, value):
    """Repeated tool output does not advance the Goal, even under a new call ID."""
    evidence = {"kind": kind, "value": value}
    if reference in goal["evidence"] or kind == "tool result" and evidence in goal["evidence"].values():
        return False
    goal["evidence"][reference] = evidence
    goal["revision"] += 1
    goal["attempts_without_progress"] = 0
    goal.pop("attempted_steps", None)
    return True


def _text(args, key, *, required=False, limit=4000):
    value = args.get(key, "")
    if not isinstance(value, str) or len(value) > limit or required and not value.strip():
        raise ValueError(f"Invalid {key}")
    return value.strip()


def _source(segment, args):
    if segment.parent_goal_id:
        if "tracker_ref" in args:
            raise ValueError("A child Goal inherits its parent assignment")
        return {"kind": "assignment", "id": segment.parent_goal_id, "slot": _slot(args)}
    if not segment.input_id or segment.internal and segment.project_ids:
        raise ValueError("A new Goal requires an accepted request or tracker intake")
    if "tracker_ref" in args:
        return {"kind": "tracker", "id": _text(args, "tracker_ref", required=True, limit=600)}
    if segment.internal:
        raise ValueError("An internal wake can open only an explicitly accepted tracker task")
    return {"kind": "input", "id": segment.input_id, "slot": _slot(args)}


def _slot(args):
    slot = args.get("slot", 1)
    if type(slot) is not int or slot < 1:
        raise ValueError("A distinct assignment in this input requires a positive slot")
    return slot


def _open(store, segment, args):
    from hermes_cli.goals import GoalContract

    contract = GoalContract(
        outcome=_text(args, "goal", required=True),
        verification=_text(args, "verification", required=True),
        constraints=_text(args, "constraints"),
    )
    source = _source(segment, args)
    key = "goal:" + hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
    initial = {
        "source": source, "contract": contract.to_dict(), "status": "ready",
        "stage": "", "next_step": "", "evidence": {}, "revision": 0,
        "checked_revision": 0, "wake_sequence": 0, "attempts_without_progress": 0,
    }
    if segment.parent_goal_id:
        initial.update(parent_goal_id=segment.parent_goal_id, orchestrator_id=segment.orchestrator_id)
    project = store.create(key, contract.outcome, "", workspace={KEY: initial})
    project_id = project["subagent_id"]
    saved = read_goal(store, project_id)
    if active_goal(saved) and saved["status"] != "blocked":
        segment.touch(project_id)
    return project_id, saved


def _checkpoint_evidence(agent, segment, args):
    fields = {"evidence_tool_call_id", "evidence_tool_name", "deliverable"} & args.keys()
    if len(fields) > 1:
        raise ValueError("Record one piece of evidence per checkpoint")
    if not fields:
        return None
    if "deliverable" in args:
        body = _text(args, "deliverable", required=True)
        return hashlib.sha256(body.encode()).hexdigest(), "deliverable", body
    field = fields.pop()
    value = _text(args, field, required=True, limit=256)
    lookup = "tool_call_id" if field == "evidence_tool_call_id" else "name"
    for row in reversed(getattr(agent, "_session_messages", ()) or ()):
        if row.get("role") != "tool" or row.get(lookup) != value or row.get("name") == "delegate_task":
            continue
        call_id, content = row.get("tool_call_id"), row.get("content")
        if call_id and call_id not in segment.history_tool_results and isinstance(content, str) and content.strip():
            return f"tool:{call_id}", "tool result", content[:6000]
    raise ValueError("No matching tool evidence was produced in this turn")


def _checkpoint(agent, store, segment, project_id, args):
    evidence = _checkpoint_evidence(agent, segment, args)
    fields = {key: _text(args, key) for key in ("stage", "next_step") if key in args}
    if "await_operation_id" in args:
        identity = _text(args, "await_operation_id", required=True, limit=128)
        if not operation_returns(store, identity):
            raise ValueError("No confirmed return path for this operation in the owning main")
        fields["operation_id"] = identity
    if not fields and evidence is None:
        raise ValueError("Record progress or the next exact step")
    if type(args.get("notify", False)) is not bool:
        raise ValueError("notify must be a boolean")

    def update(goal):
        if not active_goal(goal):
            raise ValueError("Goal is terminal")
        if goal["status"] == "blocked":
            raise ValueError("Resume the blocked Goal before recording progress")
        goal.update(fields)
        if evidence is not None:
            add_evidence(goal, *evidence)
        if "next_step" in fields:
            goal["step_revision"] = goal["revision"]
        return goal

    saved = mutate_goal(store, project_id, update)
    segment.touch(project_id)
    if args.get("notify") and evidence is not None:
        segment.notify = True
    return saved


def close_wait(store, project_id, reason):
    from tools.delegate_wait_timer import close, settle

    workspace = json.loads(store.get(project_id)["workspace_json"])
    wait = workspace.get("external_wait")
    if not wait or wait["state"] == "CLOSED":
        return
    if wait["state"] == "CLOSING":
        def mutate(change):
            return store.mutate_workspace(project_id, change)

        def authorize(current, _conn):
            if current["state"] not in {"CLOSING", "CLOSED"}:
                raise ValueError("External wait is no longer closing")
            return current["close_reason"]

        settle((project_id, wait["wait_id"], wait["job_id"]), mutate, authorize)
        return
    close(store, project_id, wait["wait_id"], {"reason": reason})


def close_goal(store, project_id, outcome, evidence=(), *, verified_revision=None):
    from tools.delegate_child_goals import close_children

    def begin(goal):
        if verified_revision is not None and (goal["revision"] != verified_revision or goal["status"] == "blocked"):
            return goal
        if goal["status"] == "closing" and goal["outcome"] != outcome:
            raise ValueError("Goal already has a different terminal result")
        if goal["status"] not in {"done", "cancelled"}:
            goal.update(status="closing", outcome=outcome, final_evidence=list(evidence))
            if verified_revision is not None:
                goal["checked_revision"] = verified_revision
                goal.pop("missing_evidence", None)
        return goal

    current = mutate_goal(store, project_id, begin, retire_pending=True,
                          when_paused="apply" if outcome == "cancelled" else "skip")
    if current["status"] != "closing":
        return current
    if outcome == "cancelled" and not current.get("parent_goal_id"):
        close_children(store, project_id)
    close_wait(store, project_id, "cancelled" if outcome == "cancelled" else "closed")

    def finish(goal):
        goal.update(status=goal["outcome"], wake_id=None)
        goal.pop("wake_step", None)
        goal.pop("wake_revision", None)
        return goal

    return mutate_goal(store, project_id, finish)


def set_goal_paused(store, project_id, paused):
    """Pause this Goal's admissions without pausing or archiving its workers."""
    from tools.delegate_project_store import goal_has_recoverable_input, set_project_paused

    def change(workspace, conn):
        goal = workspace.get(KEY)
        row = conn.execute("SELECT paused, archived FROM delegated_projects WHERE subagent_id=?", (project_id,)).fetchone()
        if not isinstance(goal, dict) or row[1] or not active_goal(goal):
            raise ValueError("Goal cannot be paused or resumed")
        if bool(row[0]) != paused:
            set_project_paused(conn, project_id, paused, False)
            # A wake already admitted before the pause cannot start this Goal later.
            goal["wake_id"] = None
            goal.pop("wake_step", None)
            goal.pop("wake_revision", None)
            if not paused and goal["status"] != "blocked":
                # A ready Goal is recoverable even if the gateway exits now. Its
                # continuation checks the saved wait, operation and worker state.
                goal.update(status="ready", attempts_without_progress=0)
                if goal_has_recoverable_input(conn, project_id):
                    # A prior recovery event may have been consumed during the
                    # pause. Mint a fresh wake for deferred or abandoned work.
                    goal["wake_sequence"] += 1
                    goal["wake_id"] = f'{project_id}:{goal["wake_sequence"]}'
                    goal["wake_step"] = "Read the queued results and recover accepted work for this saved Project Goal."
                    goal["wake_revision"] = goal["revision"]
        return goal

    return store.mutate_workspace(project_id, change, active_only=False)


def _change_status(store, segment, project_id, op, args):
    if op in {"pause", "resume", "cancel"} and not segment.parent_goal_id and (segment.internal or not segment.input_id):
        raise ValueError("This action requires a fresh request from the user")
    if op == "pause":
        return set_goal_paused(store, project_id, True)
    if op == "resume" and store.get(project_id)["paused"]:
        from hermes_runtime.adapters.goal_turn import resume_paused_goal

        resume_paused_goal(store, project_id)
    if op == "cancel":
        current = close_goal(store, project_id, "cancelled")
        segment.project_ids.add(project_id)
        segment.selected_id = None
        return current

    def change(goal):
        if not active_goal(goal):
            raise ValueError("Goal is terminal")
        if op == "block":
            goal.update(status="blocked", blocker=_text(args, "blocker", required=True),
                        unblock=_text(args, "unblock", required=True), wake_id=None)
            goal.pop("wake_step", None)
            goal.pop("wake_revision", None)
            if segment.parent_goal_id:
                goal["escalation_input_id"] = segment.input_id if args.get("notify") is True else None
        elif goal["status"] in {"blocked", "pending"}:
            goal.update(status="ready", blocker=None, unblock=None, attempts_without_progress=0)
            goal.pop("attempted_steps", None)
            goal.pop("step_revision", None)
            goal.pop("escalation_input_id", None)
        return goal

    current = mutate_goal(store, project_id, change)
    if op == "block":
        segment.project_ids.add(project_id)
        segment.selected_id = None
        segment.notify = True
    else:
        segment.touch(project_id)
    return current


def dispatch(agent, args):
    from hermes_runtime.adapters.goal_turn import current_segment, goal_in_scope
    from tools.delegate_external_wait import _owns_main

    segment = current_segment(agent)
    if segment is None or not (segment.parent_goal_id or _owns_main(agent)):
        return tool_error("Project Goals require their owning main or persistent orchestrator")
    op = args.get("op")
    if not isinstance(op, str) or op not in FIELDS or args.keys() - (FIELDS[op] | {"action", "op"}):
        return tool_error("Invalid native Project Goal operation or fields")
    try:
        if segment.parent_goal_id:
            from tools.delegate_project_store import _goal_input_state, _transaction

            with _transaction() as conn:
                if _goal_input_state(conn, segment.parent_goal_id, child_parent=True) != "open":
                    raise ValueError("The parent Goal is paused or closed")
        with ProjectStore(agent) as store:
            if op == "list":
                return json.dumps({"goals": [
                    {"project_id": p["subagent_id"], "goal": p["goal"], "status": goal["status"]}
                    for p in store.projects()
                    if not p["paused"] and not p["archived"]
                    and goal_in_scope(agent, p["subagent_id"])
                    and (goal := json.loads(p["workspace_json"]).get(KEY)) and active_goal(goal)
                ]})
            if op == "open":
                project_id, saved = _open(store, segment, args)
            else:
                project_id = _text(args, "project_id", required=True, limit=128)
                if not goal_in_scope(agent, project_id):
                    raise ValueError("Project Goal is outside the scope of this wake")
                if op == "select":
                    saved = read_goal(store, project_id)
                    if active_goal(saved) and saved["status"] != "blocked":
                        segment.touch(project_id)
                    else:
                        segment.selected_id = None
                elif op == "checkpoint":
                    saved = _checkpoint(agent, store, segment, project_id, args)
                else:
                    saved = _change_status(store, segment, project_id, op, args)
            return json.dumps({"success": True, "project_id": project_id, **saved}, ensure_ascii=False)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        return tool_error(str(error))
