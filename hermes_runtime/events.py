"""Event envelopes and content policy, independent of the execution engine."""

import json
import time


def event_prompt(prompt, source, event_id, metadata):
    envelope = {
        "event_id": event_id,
        "source": source,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metadata": metadata or {},
    }
    sections = [
        "[AgentReady event]",
        json.dumps(envelope, ensure_ascii=False, sort_keys=True),
        "",
        "[UNTRUSTED EVENT DATA]",
        prompt,
        "[/UNTRUSTED EVENT DATA]",
    ]
    return "\n".join(sections)


def combine_event_content(events):
    if all(isinstance(event, str) for event in events):
        return "\n\n".join(events)
    parts = []
    for event in events:
        parts.extend(
            event if isinstance(event, list) else [{"type": "text", "text": str(event)}]
        )
    return parts
