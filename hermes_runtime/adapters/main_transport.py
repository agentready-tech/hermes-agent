"""Private Panel-only main delivery on the existing native session event stream."""
from datetime import datetime
from pathlib import Path

from gateway.session_local import LocalSessionAdapter
from hermes_state_runtime import RuntimeStoreError


def register_platform():
    # Persisted sources must be decodable before SessionStore loads its routing index.
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermes_runtime.standalone.main_files import HINT
    platform_registry.register(PlatformEntry(name='agentready', label='AgentReady main',
        adapter_factory=_private_only, check_fn=lambda: True, source='builtin', platform_hint=HINT))


def _private_only(_config):
    raise RuntimeError('Managed main is bound by its private gateway authority')


class MainAdapter(LocalSessionAdapter):
    """Use native streaming/approval delivery, with no socket or external sender."""

    def __init__(self, authority):
        from gateway.config import Platform
        super().__init__(authority)
        self.platform = Platform('agentready')

    def authorize_source(self, source):
        saved = self.sources.get(source.chat_id)
        return (saved is not None and source.to_dict() == saved[0].to_dict()
                and any(live.source is saved[0] for live in self.authority.sessions.values()))


def authorize_source(runner, source):
    if source.platform.value != 'agentready':
        return None
    from gateway.session_authorities import all_authorities
    from hermes_runtime.adapters.hermes import enabled
    adapter = runner._adapters_for_profile(source.profile).get(source.platform)
    return (enabled() and isinstance(adapter, MainAdapter)
            and any(adapter.authority is owner for owner in all_authorities(runner))
            and adapter.authorize_source(source))


def automation_ref(authority, adapter, source, entry):
    """Only the owning private main adapter can admit a producer without messaging provenance."""
    from gateway.session_contract import SessionRef
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.adapters.state import resolve_home
    from hermes_runtime.sessions import HOME_SESSION_ID, home_session_key

    live = authority.sessions.get(HOME_SESSION_ID)
    if (not enabled() or Path(authority.profile_id).resolve() != managed_home()
            or not isinstance(adapter, MainAdapter) or adapter.authority is not authority
            or live is None or live.route != home_session_key()
            or live.source.to_dict() != source.to_dict()
            or entry.origin is None or entry.origin.to_dict() != source.to_dict()
            or entry.session_key != live.route or resolve_home(entry.session_id) is None
            or authority.runner._adapter_for_source(source) is not adapter
            or authorize_source(authority.runner, source) is not True):
        raise RuntimeStoreError('permission_denied')
    return SessionRef(authority.profile_id, HOME_SESSION_ID)


def bind_main(authority, entry):
    from gateway.session_local_recovery import local_adapter_map
    from hermes_runtime.adapters.state import resolve_home
    if resolve_home(entry.session_id) is None or entry.origin.to_dict() != main_source(authority).to_dict():
        raise RuntimeStoreError('storage_unavailable')
    adapters = local_adapter_map(authority)
    platform = entry.origin.platform
    adapter = adapters.get(platform)
    if adapter is None:
        adapter = adapters[platform] = MainAdapter(authority)
    if not isinstance(adapter, MainAdapter) or adapter.authority is not authority:
        raise RuntimeStoreError('runtime_draining')
    adapter.register_source(entry.origin)


def main_source(authority):
    from gateway.config import Platform
    from gateway.session_local_recovery import local_source
    from hermes_runtime.sessions import HOME_SESSION_ID
    source = local_source(authority, HOME_SESSION_ID, 'agentready-main')
    source.platform = Platform('agentready')
    return source


def bootstrap_main(authority):
    """Bind the existing managed transcript, never create a replacement conversation."""
    from gateway.session import SessionEntry
    from hermes_runtime.adapters.state import current_home_session_id, resolve_home
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_TITLE, home_session_key
    authority._require_admission_open()
    register_platform()
    target = current_home_session_id()
    if resolve_home(target) is None or authority.db.get_session(target) is None:
        raise RuntimeStoreError('not_found')
    source = main_source(authority)
    now = datetime.now()
    route = home_session_key()
    candidate = SessionEntry(route, target, now, now, origin=source,
                             platform=source.platform, display_name=HOME_SESSION_TITLE)
    store = authority.runner.session_store
    with store._lock:
        store._ensure_loaded_locked()
        entry = store._entries.get(route)
        if entry is not None:
            return entry
        store._entries[route] = candidate
    try:
        store._save_entries()
    except BaseException:
        with store._lock:
            if store._entries.get(route) is candidate:
                del store._entries[route]
        raise
    return candidate


def require_owner(connection):
    from hermes_runtime.adapters.hermes import managed_home
    authority = connection.authority
    if (not connection.native_owner or connection.actor.profile_id != authority.profile_id
            or Path(authority.profile_id).resolve() != managed_home()
            or 'session:read' not in connection.actor.capabilities):
        raise RuntimeStoreError('permission_denied')


def native_head_source(authority, live, payload):
    """An actual messaging head keeps its own authenticated delivery transport."""
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.sessions import home_session_key
    if (enabled() and live.route == home_session_key() and live.source.platform.value == 'agentready'):
        from gateway.session_envelope import restore_native
        return restore_native(payload, authority.runner).source
    return live.source


def restore_owner_main(authority):
    """The reserved gateway owner restores Panel-only queued work without a viewer."""
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.sessions import HOME_SESSION_ID
    if not enabled() or Path(authority.profile_id).resolve() != managed_home():
        return
    entry = bootstrap_main(authority)
    if entry.origin is not None and entry.origin.platform.value == 'agentready' and not entry.suspended:
        bind_main(authority, entry)
        authority._publish_route(entry.origin, HOME_SESSION_ID, entry.session_key)


def recover_panel_inputs(authority, sid, source, rows):
    """Resume private Panel admissions through the same native recovery/claim gates."""
    from hermes_runtime.sessions import HOME_SESSION_ID
    if sid != HOME_SESSION_ID or source.platform.value != 'agentready':
        return False
    if not authorize_source(authority.runner, source):
        raise RuntimeStoreError('permission_denied')
    from gateway.session_contract import SessionRef
    from gateway.session_operator import check_local_input
    ref = SessionRef(authority.profile_id, sid)
    for row in rows:
        if row['status'] == 'queued':
            if 'local_automation_v1' in row['payload']:
                from gateway.session_automation import check_local_automation
                check_local_automation(authority, ref, row)
            else:
                check_local_input(authority, ref, row, source=source)
    if any(row['status'] == 'unknown' for row in rows):
        raise RuntimeStoreError('unknown_execution')
    authority._schedule(ref)
    return True
