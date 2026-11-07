"""Attach managed viewers to their existing main history and native gateway FIFO."""

from gateway.session_contract import SessionRef
from hermes_state_runtime import RuntimeStoreError


def restore_main(connection, session_id):
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.adapters.state import resolve_home
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    if not enabled() or resolve_home(session_id) is None:
        return None
    # This is the same-home connector, authenticated by the gateway's private bootstrap.
    # A dashboard identity knowing a stored id must not acquire this trust.
    from hermes_runtime.adapters.main_transport import require_owner, bootstrap_main, bind_main
    require_owner(connection)
    authority = connection.authority
    if HOME_SESSION_ID not in authority.sessions:
        store = authority.runner.session_store
        with store._lock:
            store._ensure_loaded_locked()
            entry = store._entries.get(HOME_SESSION_KEY)
        if entry is None:
            entry = bootstrap_main(authority)
        if entry.origin is None or resolve_home(entry.session_id) is None:
            raise RuntimeStoreError('not_found')
        from gateway.config import Platform
        if entry.suspended or entry.origin.platform == Platform.LOCAL:
            raise RuntimeStoreError('session_busy')
        if entry.origin.platform.value == 'agentready':
            bind_main(authority, entry)
        # Existing messaging routes keep their transport; a Panel-only route shares the same
        # logical owner and compression history, without manufacturing a messaging identity.
        authority._publish_route(entry.origin, HOME_SESSION_ID, HOME_SESSION_KEY)
    return SessionRef(authority.profile_id, HOME_SESSION_ID)


def viewer_turn(turn):
    """A managed Panel admission uses the existing interactive/batched-clarify contract."""
    from hermes_runtime.adapters.hermes import enabled
    from gateway.session_cli_turn import cli_turn
    message_id = getattr(getattr(turn, '_ctx', None), 'inbound_message_id', None)
    return enabled() and cli_turn(message_id).get('clarify_batch') is True
