"""Hermes TUI wire and attachment translation."""

import time
import uuid
from agentready_runtime.adapters.state import belongs_to_home_lineage
from agentready_runtime.adapters.turns import forward_to_active_turn


def stamp_event(frame):
    if frame.get("method") == "event":
        frame.setdefault("params", {}).setdefault(
            "agentready_event_id", uuid.uuid4().hex
        )


def busy_submit(
    proceed, rid, sid, session, text, transport, queued=False, turn_author=None
):
    from tui_gateway import server

    if not belongs_to_home_lineage(session.get("session_key")) or queued:
        return proceed(
            rid, sid, session, text, transport, queued=queued, turn_author=turn_author
        )
    with session["history_lock"]:
        if not session.get("running"):
            return None
        images = list(session.get("attached_images", []))
    content = text
    if images:
        from agent.image_routing import build_native_content_parts

        content, skipped = build_native_content_parts(text, images)
        if skipped:
            return server._err(
                rid, 4004, "Cannot read attached images: " + ", ".join(skipped)
            )
    try:
        accepted = forward_to_active_turn(content)
    except (OSError, RuntimeError) as error:
        return server._err(rid, 5000, str(error))
    with session["history_lock"]:
        if not accepted and not session.get("running"):
            return None
        session["attached_images"] = [
            path for path in session.get("attached_images", []) if path not in images
        ]
        session["last_active"] = time.time()
        if not accepted:
            # Preserve upstream FIFO envelopes, attachments and authors
            # when no turn owner can accept a steer yet.
            server._enqueue_prompt(session, content, transport, turn_author=turn_author)
    return server._ok(rid, {"status": "steered" if accepted else "queued"})
