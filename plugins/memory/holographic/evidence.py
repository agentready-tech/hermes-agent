"""Evidence comes from runtime messages, never from a model-invented citation."""

import hashlib
import json


def message_sources(messages, session_id, *, roles=("user",)):
    from agent.memory_provider import is_trivial_prompt
    from agent.redact import redact_sensitive_text
    from agent.context_compressor import (
        _MERGED_PRIOR_CONTEXT_HEADER, _MERGED_SUMMARY_DELIMITER, is_compaction_summary_message,
    )

    for message in messages or []:
        role = message.get("role")
        text = message.get("content")
        if isinstance(text, list):
            text = "\n".join(part["text"] for part in text if isinstance(part, dict)
                             and part.get("type") == "text" and isinstance(part.get("text"), str))
        if role not in roles or not isinstance(text, str):
            continue
        # A merged row contains genuine text before the summary delimiter.
        if role == "user" and _MERGED_SUMMARY_DELIMITER in text:
            text = text.split(_MERGED_SUMMARY_DELIMITER, 1)[0].removeprefix(_MERGED_PRIOR_CONTEXT_HEADER).strip()
        elif is_compaction_summary_message(message):
            continue
        if text.lstrip().startswith(("[ASYNC ", "[Subagent Context]", "[Your active task list")):
            continue
        event = {}
        if text.startswith("[AgentReady event]\n"):
            header, separator, body = text.partition("\n\n")
            try:
                event = json.loads(header.split("\n", 1)[1])
            except (ValueError, IndexError):
                continue
            if not separator or event.get("source") == "schedule":
                continue
            text = body
        text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
        if not text.strip() or text.lstrip().startswith("/") or (role == "user" and is_trivial_prompt(text)):
            continue
        message_id = message.get("_row_id", message.get("id", message.get("tool_call_id")))
        for offset in range(0, len(text), 4000):
            quote = text[offset:offset + 4000]
            locator = str(message_id) if message_id is not None else hashlib.sha256(text.encode()).hexdigest()
            identity = json.dumps([session_id, locator, event.get("event_id"), offset], ensure_ascii=False)
            yield {"kind": "conversation" if role == "user" else "tool_result", "role": role,
                   "session_id": session_id, "message_id": str(message_id) if message_id is not None else None,
                   "event_id": event.get("event_id"), "channel": event.get("source"), "offset": offset,
                   "uri": f"hermes://sessions/{session_id}/messages/{locator}#{offset}",
                   "event_key": hashlib.sha256(identity.encode()).hexdigest(), "quote": quote}


def tool_sources(args, messages, session_id):
    sources = list(message_sources(messages, session_id, roles=("user", "tool")))
    quote = args.get("source_quote")
    if quote is not None:
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("source_quote must be a non-empty exact quote")
        for source in reversed(sources):
            if quote in source["quote"]:
                return [{**source, "quote": quote}]
        raise ValueError("source_quote was not found in the actual conversation or tool results")
    user_sources = [source for source in sources if source["role"] == "user"]
    return user_sources[-1:] or [{"kind": "agent_record", "session_id": session_id, "quote": ""}]
