"""Apply an explicit Panel profile selection at the existing main turn boundary."""
from dataclasses import replace
import re


_RECEIPT = 'panel_model_selection_revision'


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


def resolve_turn_runtime(runner, source, session_key, user_config):
    apply_selection(runner, source, session_key, selection_revision(user_config))
    return runner._resolve_session_agent_runtime(source=source, session_key=session_key, user_config=user_config)
