"""Append addressed mail at safe native loop boundaries, preserving the cached prefix."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def eligible(agent):
    from hermes_constants import get_hermes_home
    db = getattr(agent, "_session_db", None)
    path = getattr(db, "db_path", None)
    names = getattr(agent, "valid_tool_names", ())
    return (isinstance(path, (str, Path)) and Path(path).resolve() == (get_hermes_home() / "state.db").resolve()
            and isinstance(names, (list, tuple, set, frozenset)) and "delegate_task" in names)


def has_mail(agent, *, ordinary_only=False):
    if not eligible(agent):
        return False
    replayed = getattr(agent, "_pending_cli_user_message", None)
    if isinstance(replayed, dict) and replayed.get("display_kind") == "agent_message":
        # Recovery has already admitted this question. Let its parent answer before
        # joining descendants that may be blocked waiting for that very answer.
        return True
    from tools.delegate_mail_store import MailStore
    with MailStore(agent) as store:
        store.acknowledge_recorded()
        return bool(store.pending(ordinary_only=ordinary_only))


def deliver(agent, messages, *, final_message=None):
    """Never mutate a cached/persisted row; a deferred final answer becomes an interim assistant row."""
    if not eligible(agent) or not messages or getattr(agent, "_interrupt_requested", False):
        return False
    from tools.delegate_mail_store import MailStore
    from agent.message_metadata import append_message
    with MailStore(agent) as store:
        live_ids = {identity for m in messages if (identity := m.get("message_id") or m.get("platform_message_id"))}
        live_ids.update(m.get("display_metadata", {}).get("agent_mail_delivery_id") for m in messages
                        if isinstance(m.get("display_metadata"), dict))
        live_ids.discard(None)
        last = messages[-1]
        safe_tail = last.get("role") == "tool" or (last.get("role") == "assistant" and not last.get("tool_calls"))
        if final_message is None and not safe_tail:
            return False
        row = store.delivery(live_ids)
        if row is not None:
            if final_message is not None:
                append_message(messages, {**final_message, "finish_reason": "agent_message_pending"})
            append_message(messages, row)
            agent._session_messages = messages
        # A failed previous flush leaves the same row live; retry it before acknowledging mail.
        if row is not None or any(p["delivery_id"] in live_ids for p in store.pending()):
            try:
                agent._flush_messages_to_session_db(messages)
                store.acknowledge_recorded()
            except Exception:
                logger.warning("Agent mail remains pending until native history is durable", exc_info=True)
        return row is not None


def dispatch(actor, args, context):
    """A native tool occurrence is the idempotency key when the model supplied none."""
    if args.get("action") == "message" and not args.get("event_id"):
        from agent.message_metadata import index_tool_call_uids, resolve_tool_call_uid
        assistant = next((m for m in reversed(context.messages or []) if m.get("tool_calls")), {})
        index = {}
        index_tool_call_uids(index, assistant)
        uid = resolve_tool_call_uid(index, context.tool_call_id)
        if uid:
            args = {**args, "event_id": "tool-mail:" + uid}
    return actor._dispatch_delegate_task(args)
