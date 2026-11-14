"""Explicit profile ownership for one managed runtime or a shared profile host."""

from pathlib import Path

_home: Path | None = None
_hosted_homes: frozenset[Path] = frozenset()
_shared_root: Path | None = None


def activate_profiles(homes) -> None:
    """Pin the complete owned set before a shared host starts serving profiles."""
    global _hosted_homes
    resolved = frozenset(Path(home).resolve() for home in homes)
    if len(resolved) < 2 or any(not home.is_dir() for home in resolved):
        raise ValueError("A shared host requires distinct existing profile homes")
    if _shared_root is not None or _home is not None or (_hosted_homes and _hosted_homes != resolved):
        raise RuntimeError("Managed runtime ownership is already configured")
    _hosted_homes = resolved
    from hermes_runtime.adapters.main_transport import register_platform
    register_platform()


def activate_profile_host(root: Path) -> None:
    """Enable managed homes under the native gateway's reserved profiles directory."""
    global _shared_root
    resolved = root.resolve()
    if _home is not None or _hosted_homes or (_shared_root is not None and _shared_root != resolved):
        raise RuntimeError("Managed runtime ownership is already configured")
    _shared_root = resolved
    from hermes_runtime.adapters.main_transport import register_platform
    register_platform()


def activate(home: Path) -> None:
    global _home
    resolved = home.resolve()
    if shared_host():
        if not owns_home(resolved):
            raise RuntimeError("The profile is not owned by this shared host")
        return
    if _home is not None and _home != resolved:
        raise RuntimeError("A managed process owns exactly one AgentReady home")
    _home = resolved
    from hermes_runtime.adapters.main_transport import register_platform
    register_platform()


def shared_host() -> bool:
    return _shared_root is not None or bool(_hosted_homes)


def enabled() -> bool:
    from hermes_constants import get_hermes_home

    return owns_home(get_hermes_home())


def owns_home(home) -> bool:
    """Whether an explicit runtime home belongs to this managed process."""
    active = Path(home).resolve()
    if _shared_root is not None:
        from gateway.runtime_ownership import process_ownership
        return (active.parent == _shared_root / "profiles" and not active.name.startswith(".")
                and process_ownership.owns(active))
    return active in _hosted_homes if _hosted_homes else active == _home


def managed_home() -> Path:
    if shared_host():
        from hermes_constants import get_hermes_home

        active = Path(get_hermes_home()).resolve()
        if not owns_home(active):
            raise RuntimeError("No owned managed profile is bound to this activity")
        return active
    if _home is None:
        raise RuntimeError("AgentReady runtime has not been activated")
    return _home
