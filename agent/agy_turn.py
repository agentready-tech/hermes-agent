"""Guard the existing native dispatch, never execute from the provider adapter."""

from agent.message_sanitization import coalesce_tool_call_id
from providers.agy_policy import AgyError


def _history_ids(rows):
    for row in rows:
        if row.get("role") == "assistant":
            yield from (coalesce_tool_call_id(call) for call in row.get("tool_calls") or ())
        elif row.get("role") == "tool":
            yield row.get("tool_call_id")


def reject_proposal(agent, calls, messages):
    if agent._interrupt_requested:
        return AgyError("cancelled")
    if len(calls) != 1 or calls[0].function.name not in agent.valid_tool_names:
        return AgyError("protocol")
    call_id = coalesce_tool_call_id(calls[0])
    if not call_id or call_id != calls[0].id:
        return AgyError("protocol")
    admitted = agent._agy_admitted_call_ids
    if call_id in admitted or call_id in _history_ids(messages):
        return AgyError("duplicate_tool_call")
    db = agent._session_db
    if db is not None:
        rows = db.get_messages_as_conversation(agent.session_id, include_ancestors=True, include_compacted=True)
        if call_id in _history_ids(rows):
            return AgyError("duplicate_tool_call")
    admitted.add(call_id)
    return None
