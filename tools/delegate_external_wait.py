"""Native main selection of one durable wait in an existing delegated project."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from tools.registry import tool_error

_KEY = "external_wait"
_FIELDS = {
    "open": {"mode", "reply_criterion", "deadline", "return_path"},
    "select": {"wait_id"},
    "close": {"wait_id", "reason", "reply_ref"},
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


def native_schema(agent, tool_defs=None, *, enabled_toolsets=None, disabled_toolsets=None):
    """Copy only the native main's schema, not the registry/MCP definition."""
    tool_defs = agent.tools if tool_defs is None else tool_defs
    names = {tool["function"]["name"] for tool in tool_defs}
    if "delegate_task" not in names or not _owns_main(agent):
        return tool_defs
    from tools.delegate_project_goal import FIELDS as goal_operations, schema_fields

    if "cronjob_manage" not in names:
        from model_tools import get_tool_definitions

        # The bound operation needs live main state. Keep its native entry available
        # even when Tool Search defers the ordinary registry schema.
        available = get_tool_definitions(
            enabled_toolsets=enabled_toolsets if enabled_toolsets is not None else agent.enabled_toolsets,
            disabled_toolsets=disabled_toolsets if disabled_toolsets is not None else agent.disabled_toolsets,
            quiet_mode=True, skip_tool_search_assembly=True,
        )
        tool_defs = [*tool_defs, *(tool for tool in available if tool["function"]["name"] == "cronjob_manage")]
    fields = {
        "op": {"type": "string", "enum": sorted(_FIELDS.keys() | goal_operations.keys())},
        "project_id": {"type": "string", "description": "Exact ID of an existing parent-owned project"},
        "wait_id": {"type": "string", "description": "Required for select and close"},
        "mode": {"type": "string", "enum": ["resume_main"]},
        "reply_criterion": {"type": "string"},
        "deadline": {"type": "string", "description": "Finite timezone-aware ISO timestamp"},
        "return_path": {"type": "string", "enum": ["timer"]},

        "reason": {"type": "string", "enum": ["reply", "cancelled", "closed", "expired"]},
        "reply_ref": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["session_admission"]}, "admission_id": {"type": "string"},
        }, "required": ["kind", "admission_id"], "additionalProperties": False},
        **schema_fields(),
    }
    result = []
    for tool in tool_defs:
        fn = tool["function"]
        if fn["name"] == "delegate_task":
            params = fn["parameters"]
            props = {**params["properties"], **fields}
            action = props["action"]
            props["action"] = {**action, "description": action["description"] +
                " Native main only: project_goal opens and continues accepted assignments. "
                "external_wait accepts op=open/select/close for an existing project."}
            tool = {**tool, "function": {**fn, "parameters": {**params, "properties": props}}}
        elif fn["name"] == "cronjob_manage":
            params = fn["parameters"]
            props = {**params["properties"], "project_id": fields["project_id"], "wait_id": {
                "type": "string", "description": "With project_id on create: bind to the exact selected external wait"}}
            tool = {**tool, "function": {**fn, "parameters": {**params, "properties": props}}}
        result.append(tool)
    return result


def timer_tool(function_name, arguments):
    if function_name == "cronjob_manage" and ("project_id" in arguments or "wait_id" in arguments):
        from tools.delegate_wait_timer import create

        return lambda agent, args, _context: create(agent, args)
    return None


def _open_fields(args):
    mode, criterion, deadline = (args.get(key) for key in ("mode", "reply_criterion", "deadline"))
    if mode != "resume_main" or not _text(criterion, 2000) or not _text(deadline, 64):
        raise ValueError("Expected a mode, reply criterion and timezone-aware deadline")
    end = datetime.fromisoformat(deadline)
    if end.tzinfo is None or end <= datetime.now(UTC):
        raise ValueError("Expected a future timezone-aware deadline")
    if args.get("return_path") != "timer":
        raise ValueError("A timer must be explicitly selected; an event return path requires confirmed native evidence")
    return {"mode": mode, "reply_criterion": criterion, "deadline": end.astimezone(UTC).isoformat(), "return_path": "timer"}


def _open(workspace, conn, fields):
    current = workspace.get(_KEY)
    if current is not None and current.get("state") != "CLOSED":
        if current["state"] == "CLOSING" or any(current.get(k) != v for k, v in fields.items()):
            raise ValueError("This project already has a different or closing wait")
        return current
    current = {**fields, "state": "OPEN", "wait_id": uuid4().hex, "job_id": None}
    if fields['mode'] == 'resume_main':
        current['input_watermarks'] = _input_watermarks(conn)
    workspace[_KEY] = current
    return current


def _input_watermarks(conn):
    rows = conn.execute("SELECT name, seq FROM sqlite_sequence WHERE name IN ('session_admissions', 'messages')").fetchall()
    result = {"session_admissions": 0, "messages": 0}
    seen = set()
    for name, seq in rows:
        if name in seen or type(seq) is not int or seq < 0:
            raise ValueError("Native input sequence is unavailable")
        seen.add(name)
        result[name] = seq
    if ("session_admissions" not in seen and conn.execute("SELECT 1 FROM session_admissions LIMIT 1").fetchone()
            or "messages" not in seen and conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()):
        raise ValueError("Native input sequence is unavailable")
    return result


def _existing(workspace, wait_id):
    current = workspace.get(_KEY)
    if not isinstance(current, dict) or current.get("wait_id") != wait_id or current.get("state") == "CLOSED":
        raise ValueError("No current wait with this exact ID")
    return current


def validate_close(current, conn, store, args):
    from hermes_runtime.adapters.external_wait_reply import proves_reply

    reason = args.get('reason')
    if reason == 'reply':
        if not proves_reply(conn, store.db_path.resolve().parent, store.parent_id, current, args.get('reply_ref')):
            raise ValueError("Closing on a reply requires trusted evidence of the original input")
    elif reason == "access_lost":
        raise ValueError("No trusted loss-of-access receipt is available for this wait")
    elif reason not in ("cancelled", "closed", "expired") or 'reply_ref' in args:
        raise ValueError("Invalid close reason or unexpected reply evidence")
    if reason == "expired" and datetime.fromisoformat(current["deadline"]) > datetime.now(UTC):
        raise ValueError("The wait has not expired")
    return reason


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
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
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
        from tools.delegate_wait_timer import close

        return close(store, project_id, wait_id, args)
    current = _existing(store.read_workspace(project_id), wait_id)
    if current["state"] == "CLOSING" or datetime.fromisoformat(current["deadline"]) <= datetime.now(UTC):
        raise ValueError("Cannot select a closing or expired wait")
    return current


def selected_timer_proof(agent):
    """None means the AGE-6 timer check does not apply to this turn."""
    from cron.wait_evidence import has_live_timer
    from cron.wait_binding import store_home
    from tools.delegate_project_store import ProjectStore

    pair = getattr(getattr(agent, '_agentready_active_turn', None), 'external_wait', None)
    if pair is None or not _owns_main(agent):
        return None
    project_id, wait_id = pair
    try:
        with ProjectStore(agent) as store:
            wait = _existing(store.read_workspace(project_id), wait_id)
        now = datetime.now(UTC)
        deadline = datetime.fromisoformat(wait['deadline'])
    except (KeyError, ValueError, TypeError):
        return None
    if (wait.get('state') not in ('OPEN', 'RESERVED', 'BOUND') or wait.get('return_path') != 'timer'
            or deadline.tzinfo is None or deadline <= now):
        return None
    try:
        store_home()
    except (ValueError, RuntimeError, OSError):
        return False
    return has_live_timer(wait, project_id, now)
