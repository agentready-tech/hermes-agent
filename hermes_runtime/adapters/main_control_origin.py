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
    if source.platform in (Platform.LOCAL, Platform.API_SERVER) or runner._adapter_for_source(source) is None:
        return {}
    # Relay custody can only come from its authenticated ingress, never a viewer.
    if source.delivered_via_upstream_relay:
        return {}
    from gateway.session_ingress_context import _binding
    from gateway.session_envelope import _snapshot_native
    from gateway.platforms.event import MessageEvent
    _, _, runtime_home, provenance = _binding(runner, source, runner._adapter_profile_for_source(source))
    if runtime_home != home:
        raise RuntimeStoreError('profile_mismatch')
    origin = _snapshot_native(runner, MessageEvent(text='', source=copy(source)), provenance)
    return {'native_origin_v1': origin['native_text_v1']}
