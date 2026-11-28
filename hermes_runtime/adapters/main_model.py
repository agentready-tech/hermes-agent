"""Apply an explicit Panel profile selection at the existing main turn boundary."""
from dataclasses import replace
import re
import threading


_RECEIPT = 'panel_model_selection_revision'
_TICKET_SLOTS_INIT = threading.Lock()


def _ticket_slots(runner):
    """Create the runner's per-session ticket store once; concurrent gateway turns share it."""
    slots = getattr(runner, "_panel_tickets", None)
    if slots is None:
        with _TICKET_SLOTS_INIT:
            slots = getattr(runner, "_panel_tickets", None)
            if slots is None:
                lock = threading.Lock()
                # Lock first: a fast-path reader that sees the dict must already see its lock.
                runner._panel_tickets_lock = lock
                slots = {}
                runner._panel_tickets = slots
    return slots, runner._panel_tickets_lock


def stash_panel_ticket(runner, session_key, ticket):
    """Publish or clear the fence for exactly one logical session."""
    if not session_key:
        return
    slots, lock = _ticket_slots(runner)
    with lock:
        if ticket is None:
            slots.pop(session_key, None)
        else:
            slots[session_key] = ticket


def take_panel_ticket(runner, session_key):
    """Atomically consume this session's ticket; a foreign turn never sees it."""
    if not session_key:
        return None
    slots, lock = _ticket_slots(runner)
    with lock:
        return slots.pop(session_key, None)


def _consume_selection(store, session_key, revision):
    # Same persistence-before-publication seam as SessionStore.set_model_override;
    # the clear and its consumed receipt must be one durable routing snapshot.
    with store._lock:
        entry = store._entry_locked(session_key)
        if entry is None or entry.metadata.get(_RECEIPT) == revision:
            return False
        data, generation = store._snapshot_routing_locked()
        entry = store._entries[session_key]
        metadata = {**entry.metadata, _RECEIPT: revision}
        data[session_key] = replace(entry, model_override=None, metadata=metadata).to_dict()
        store._persist_routing_data(data, generation)
        entry.model_override = None
        entry.metadata = metadata
    return True


def selection_revision(user_config):
    return ((user_config or {}).get('autonomous') or {}).get('panel', {}).get('model_selection_revision')


def apply_selection(runner, source, session_key, revision):
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.sessions import home_session_key
    from hermes_cli.profiles import profile_matches_home

    own_main = (enabled() and session_key == home_session_key()
                and (not source.profile or profile_matches_home(source.profile, managed_home())))
    if (own_main and isinstance(revision, str) and re.fullmatch(r'[a-f0-9]{32}', revision)
            and _consume_selection(runner.session_store, session_key, revision)):
        runner._session_model_overrides.pop(session_key, None)
        runner._pending_one_turn_model_restores.pop(session_key, None)
        runner._evict_cached_agent(session_key)
        return True
    return False


def current_main_selection(source, session_key, user_config):
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.sessions import home_session_key
    from hermes_runtime.standalone.agy_selection import read_selection
    from hermes_cli.profiles import profile_matches_home

    if enabled() and session_key == home_session_key() and (
            not source.profile or profile_matches_home(source.profile, managed_home())):
        return read_selection(managed_home(), user_config)
    return None


def resolve_turn_runtime(runner, source, session_key, user_config):
    ticket = current_main_selection(source, session_key, user_config)
    stash_panel_ticket(runner, session_key, None)
    if ticket is not None:
        ticket.require_saved()
    from providers.agy_policy import AgyError, is_agy
    model_config = (user_config or {}).get("model")
    if isinstance(model_config, dict) and is_agy(model_config.get("provider")):
        # Check the reserved configured route before any cached override, launch policy,
        # channel routing or generic default could replace it.
        from hermes_runtime.standalone.agy_runtime import runtime
        if ticket is None:
            raise AgyError("unavailable")
        runtime(ticket.model)
    transitioned = apply_selection(runner, source, session_key, selection_revision(user_config))
    if ticket is not None and is_agy(ticket.provider):
        stash_panel_ticket(runner, session_key, ticket)
        from gateway.run import _runtime_agent_kwargs
        return ticket.model, _runtime_agent_kwargs(runtime(ticket.model))
    if ticket is not None and transitioned:
        # One-shot return to another source; a later explicit /model owns the session again.
        stash_panel_ticket(runner, session_key, ticket)
    return runner._resolve_session_agent_runtime(source=source, session_key=session_key, user_config=user_config)
