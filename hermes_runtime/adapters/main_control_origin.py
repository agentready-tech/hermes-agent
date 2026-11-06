"""Private connector custody for a BotMode task submitted before its first platform message.

This is a route reference on an authenticated control admission, not a fabricated
Telegram input. Completion and restart reuse the ordinary native validation.
"""
from copy import copy
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError


def capture_origin(authority, ref, source):
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.adapters.state import resolve_home
    from gateway.config import Platform
    if not enabled() or resolve_home(ref.session_id) is None:
        return {}
    home = managed_home()
    if Path(authority.profile_id).resolve() != home:
        raise RuntimeStoreError('profile_mismatch')
    runner = authority.runner
    if source.platform in (Platform.LOCAL, Platform.API_SERVER):
        return {}
    adapter = runner._adapter_for_source(source)
    if adapter is None:
        return {}
    # Relay custody can only come from its authenticated ingress, never a viewer.
    if source.delivered_via_upstream_relay:
        return {}
    from gateway.session_ingress_context import _binding
    from gateway.session_envelope import _snapshot_native
    from gateway.session_identity import identity_of
    from gateway.platforms.event import MessageEvent
    registered, transport_profile = runner._owning_profile(adapter, source.platform)
    if not registered:
        raise RuntimeStoreError('not_found')
    _, _, runtime_home, provenance = _binding(runner, source, transport_profile)
    if runtime_home != home:
        raise RuntimeStoreError('profile_mismatch')
    frozen = copy(source)
    identity = identity_of(frozen)
    # A control-created turn inferred the primary without receiving a platform callback.
    # The native delivery registry above is authoritative; revalidate only this copy under it.
    if identity is not None and identity.transport_inferred:
        frozen._identity = None
    origin = _snapshot_native(runner, MessageEvent(text='', source=frozen), provenance)
    return {'native_origin_v1': origin['native_text_v1']}
