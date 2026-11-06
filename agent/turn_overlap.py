"""Profile/database-owned diagnostic for overlapping transcript writers."""
import logging
from pathlib import Path
import threading
import time

# Preserve the existing diagnostic channel while moving its ownership into one module.
logger = logging.getLogger('agent.agent_runtime_helpers')

# Session-scoped in-flight registry for note_turn_start: the gateway caches agents per routing key
# while the transcript is keyed by session_id, so two agent objects can run concurrent turns on one
# session unseen by per-agent state.
_INFLIGHT_TURNS_BY_SESSION: dict[tuple[str, str], tuple[str, float]] = {}
_INFLIGHT_TURNS_LOCK = threading.Lock()


def _turn_store_key(agent, session_id):
    from hermes_constants import hermes_home_key
    path = getattr(getattr(agent, '_session_db', None), 'db_path', None)
    home = Path(path).parent if path else None
    return hermes_home_key(home), str(session_id)


def note_turn_start(agent, turn_id: str):
    """Tripwire: warn when a turn starts while a previous turn of the same agent or session
    (on another agent object) has not finished its persist. Does not prevent the overlap; it
    names both turn ids so the dispatch route that bypassed the busy guard is findable in logs.
    Returns the previous in-flight turn_id on overlap, else None; takes the slot either way."""
    prev = getattr(agent, "_inflight_turn_id", None)
    prev_started = getattr(agent, "_inflight_turn_started", 0.0)
    agent._inflight_turn_id = turn_id
    agent._inflight_turn_started = time.time()
    overlap = None
    if prev and prev != turn_id:
        logger.warning(
            "turn %s starting while turn %s (started %.0fs ago) has not "
            "completed its turn-end persist (session=%s) — concurrent turns "
            "on one session; transcript writes may interleave", turn_id, prev,
            time.time() - prev_started if prev_started else -1.0, getattr(agent, "session_id", None) or "-",
        )
        overlap = prev
    # Cross-agent leg: same session_id in flight under another agent object (busy guard is keyed by
    # routing key and cannot see it). Persist-disabled forks share the parent's session_id but never
    # write, so they must not register or pop here (note_turn_persisted skips them symmetrically).
    session_id = getattr(agent, "session_id", None)
    if session_id and not getattr(agent, "_persist_disabled", False):
        now = time.time()
        key = _turn_store_key(agent, session_id)
        with _INFLIGHT_TURNS_LOCK:
            entry = _INFLIGHT_TURNS_BY_SESSION.get(key)
            _INFLIGHT_TURNS_BY_SESSION[key] = (turn_id, now)
        # Record the session id registered under: compression can rotate agent.session_id mid-turn
        # and persist must pop the slot actually held.
        agent._inflight_turn_session_id = session_id
        agent._inflight_turn_store_key = key
        if entry and entry[0] not in (turn_id, prev):
            logger.warning(
                "turn %s starting while turn %s (started %.0fs ago) is still "
                "in flight on session %s under a different agent object — "
                "two routing keys are mapped to one session_id; concurrent "
                "turns on one session; transcript writes may interleave", turn_id, entry[0],
                now - entry[1] if entry[1] else -1.0, session_id,
            )
            overlap = overlap or entry[0]
    return overlap


def note_turn_persisted(agent):
    """Clear the in-flight marker at turn-end persist (see note_turn_start). Unconditional by
    design: on a real overlap the first persist clears the second slot, so the tripwire
    under-reports rather than double-reports."""
    agent._inflight_turn_id = None
    # Persist-disabled forks never registered a slot; popping here would steal the live parent
    # turn's slot (symmetric with note_turn_start).
    if not getattr(agent, "_persist_disabled", False):
        key = getattr(agent, "_inflight_turn_store_key", None)
        session_id = getattr(agent, "_inflight_turn_session_id", None) or getattr(agent, "session_id", None)
        if key is None and session_id:
            key = _turn_store_key(agent, session_id)
        if key:
            with _INFLIGHT_TURNS_LOCK:
                _INFLIGHT_TURNS_BY_SESSION.pop(key, None)
    agent._inflight_turn_session_id = None
    agent._inflight_turn_store_key = None
