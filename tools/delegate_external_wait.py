"""Native main selection of one durable wait in an existing delegated project."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from tools.registry import tool_error

_KEY = "external_wait"
_FIELDS = {
    "open": {"mode", "reply_criterion", "deadline", "return_path", "source_ref"},
    "select": {"wait_id"},
    "close": {"wait_id", "reason"},
}


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()


def _owns_main(agent):
    from agent.delegation_context import is_delegated_child_context
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.adapters.state import resolve_home

    return (enabled() and not getattr(agent, "side_agent", False) and not getattr(agent, "_persist_disabled", False)
            and not getattr(agent, "_interrupt_requested", False)
            and getattr(agent, "platform", None) not in {"cron", "subagent"}
            and not is_delegated_child_context() and resolve_home(agent.session_id) is not None)


def native_schema(agent):
    """Copy only the native main's schema, not the registry/MCP definition."""
    if not _owns_main(agent):
        return agent.tools
    fields = {
        "op": {"type": "string", "enum": sorted(_FIELDS)},
        "project_id": {"type": "string", "description": "Exact ID of an existing parent-owned project"},
        "wait_id": {"type": "string", "description": "Required for select and close"},
        "mode": {"type": "string", "enum": ["resume_main", "browser_only"]},
        "reply_criterion": {"type": "string"},
        "deadline": {"type": "string", "description": "Finite timezone-aware ISO timestamp"},
        "return_path": {"type": "string", "enum": ["timer"]},
        "source_ref": {"type": "string", "description": "Exact authorized source; browser-only"},
        "reason": {"type": "string", "enum": ["cancelled", "closed", "expired", "access_lost"]},
    }
    result = []
    for tool in agent.tools:
        fn = tool["function"]
        if fn["name"] == "delegate_task":
            params = fn["parameters"]
            props = {**params["properties"], **fields}
            action = props["action"]
            props["action"] = {**action, "description": action["description"] +
                " Native main only: external_wait accepts op=open/select/close for an existing project."}
            tool = {**tool, "function": {**fn, "parameters": {**params, "properties": props}}}
        result.append(tool)
    return result


def _open_fields(args):
    mode, criterion, deadline = (args.get(key) for key in ("mode", "reply_criterion", "deadline"))
    if mode not in ("resume_main", "browser_only") or not _text(criterion, 2000) or not _text(deadline, 64):
        raise ValueError("Expected a mode, reply criterion and timezone-aware deadline")
    end = datetime.fromisoformat(deadline)
    if end.tzinfo is None or end <= datetime.now(UTC):
        raise ValueError("Expected a future timezone-aware deadline")
    if args.get("return_path") != "timer":
        raise ValueError("A timer must be explicitly selected; an event return path requires confirmed native evidence")
    fields = {"mode": mode, "reply_criterion": criterion, "deadline": end.astimezone(UTC).isoformat(), "return_path": "timer"}
    if mode == "browser_only":
        if not _text(args.get("source_ref"), 2000):
            raise ValueError("Browser-only waits require one exact authorized source")
        fields["source_ref"] = args["source_ref"]
    elif "source_ref" in args:
        raise ValueError("Resume-main waits do not take a source address")
    return fields


def _open(workspace, conn, fields):
    current = workspace.get(_KEY)
    if current is not None and current.get("state") != "CLOSED":
        if current["state"] == "CLOSING" or any(current.get(k) != v for k, v in fields.items()):
            raise ValueError("This project already has a different or closing wait")
        return current
    watermark = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    current = {**fields, "state": "OPEN", "wait_id": uuid4().hex, "job_id": None, "input_watermark": watermark}
    workspace[_KEY] = current
    return current


def _existing(workspace, wait_id):
    current = workspace.get(_KEY)
    if not isinstance(current, dict) or current.get("wait_id") != wait_id or current.get("state") == "CLOSED":
        raise ValueError("No current wait with this exact ID")
    return current


def _close(workspace, wait_id, reason):
    current = _existing(workspace, wait_id)
    if reason not in ("cancelled", "closed", "expired", "access_lost"):
        raise ValueError("Closing on a reply requires trusted evidence of the original input")
    if reason == "expired" and datetime.fromisoformat(current["deadline"]) > datetime.now(UTC):
        raise ValueError("The wait has not expired")
    if current["state"] != "CLOSING":
        current.update(state="CLOSING" if current["job_id"] is not None else "CLOSED", close_reason=reason)
    return current


def dispatch(agent, args):
    """Handle an explicit native action; the shared registry/MCP handler does not dispatch it."""
    from tools.delegate_project_store import ProjectStore

    if getattr(agent, "_agentready_active_turn", None) is None or not _owns_main(agent):
        return tool_error("External waits require a live owning managed-main turn")
    op, project_id = args.get("op"), args.get("project_id")
    try:
        if not isinstance(op, str) or op not in _FIELDS or not _text(project_id, 128):
            raise ValueError("Expected open, select or close with an exact project_id")
        if args.keys() - ({"action", "op", "project_id"} | _FIELDS[op]):
            raise ValueError("Unexpected fields for this external wait operation")
        with ProjectStore(agent) as store:
            current = _apply(store, project_id, op, args)
    except ValueError as error:
        return tool_error(str(error))
    turn = agent._agentready_active_turn
    pair = (project_id, current["wait_id"])
    if op in ("open", "select"):
        turn.external_wait = pair
    elif getattr(turn, "external_wait", None) == pair:
        turn.external_wait = None
    return json.dumps({"success": True, "project_id": project_id, **current})


def _apply(store, project_id, op, args):
    if op == "open":
        fields = _open_fields(args)
        return store.mutate_workspace(project_id, lambda workspace, conn: _open(workspace, conn, fields))
    wait_id = args.get("wait_id")
    if not _text(wait_id, 128):
        raise ValueError("Expected an exact wait_id")
    if op == "close":
        return store.mutate_workspace(project_id, lambda workspace, _conn: _close(workspace, wait_id, args.get("reason")))
    current = _existing(store.read_workspace(project_id), wait_id)
    if current["state"] == "CLOSING" or datetime.fromisoformat(current["deadline"]) <= datetime.now(UTC):
        raise ValueError("Cannot select a closing or expired wait")
    return current
