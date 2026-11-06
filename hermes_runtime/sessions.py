"""Engine-independent continuous-session policy."""

HOME_SESSION_ID = "agentready_home"
HOME_SESSION_TITLE = "Bot Chat"
HOME_SESSION_KEY = "agent:main:agentready:home"


def home_session_key():
    """One home conversation per profile, with a unique in-process routing lane."""
    from hermes_runtime.adapters.hermes import managed_home, shared_host

    if not shared_host():
        return HOME_SESSION_KEY
    from gateway.session import _session_key_namespace
    from hermes_constants import named_profile_home

    home = managed_home()
    named = named_profile_home(home)
    if named is None:
        raise RuntimeError("A shared managed runtime requires a named profile")
    return _session_key_namespace(home.name) + ":agentready:home"
