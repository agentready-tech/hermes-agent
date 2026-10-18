"""Existing live controls, persistent-task controls and coordination share one facade."""

import json

from tools.registry import tool_error


def control_action(parent, action, subagent_id, message, event_id, query, timeout_seconds):
    from tools.delegate_tool import _CONTROL_ACTIONS, _handle_control_action
    from tools.delegate_projects import include_projects, project_action
    try:
        if action in ("message", "peers", "wait"):
            from tools.delegate_mail import mail_action
            return json.dumps(mail_action(parent, action, subagent_id, message, event_id, timeout_seconds), ensure_ascii=False)
        if action in ("send", "read", "archive") or (action == "stop" and str(subagent_id or "").startswith("project-")):
            return json.dumps(project_action(parent, action, subagent_id=subagent_id, message=message, event_id=event_id), ensure_ascii=False)
        if action == "list":
            payload = json.loads(_handle_control_action(action, subagent_id, message, parent))
            return json.dumps(include_projects(parent, payload, query), ensure_ascii=False)
        if action in _CONTROL_ACTIONS:
            return _handle_control_action(action, subagent_id, message, parent)
    except (ValueError, TypeError) as exc:
        return tool_error(str(exc))
    return None
