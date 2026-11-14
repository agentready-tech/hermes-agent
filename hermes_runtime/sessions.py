"""Engine-independent continuous-session policy."""

HOME_SESSION_ID = "agentready_home"
HOME_SESSION_TITLE = "Bot Chat"
HOME_SESSION_KEY = "agent:main:agentready:home"


def home_session_key(home=None, *, shared=None):
    """One home conversation per profile, with a unique in-process routing lane."""
    from hermes_runtime.adapters.hermes import managed_home, shared_host

    if shared is None:
        shared = shared_host()
    if not shared:
        return HOME_SESSION_KEY
    from gateway.session import _session_key_namespace
    from hermes_constants import named_profile_home

    home = managed_home() if home is None else home
    named = named_profile_home(home)
    if named is None:
        raise RuntimeError("A shared managed runtime requires a named profile")
    return _session_key_namespace(home.name) + ":agentready:home"


def managed_route_key(source, profile=None):
    """Resolve an owned BotMode lane at ingress, before a turn has bound its home."""
    from gateway.config import Platform
    from gateway.session_identity import identity_of
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters.hermes import owns_home, shared_host

    if source.platform == Platform.LOCAL:
        return None
    identity = identity_of(source)
    named = profile or getattr(source, 'profile', None)
    if identity is not None:
        home = identity.runtime_home
    elif shared_host() and named:
        from hermes_cli.profiles import get_profile_dir
        home = get_profile_dir(named)
    else:
        home = get_hermes_home()
    return home_session_key(home) if owns_home(home) else None
