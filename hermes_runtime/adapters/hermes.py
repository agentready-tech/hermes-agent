"""Explicit profile ownership for one managed runtime or a shared profile host."""

from pathlib import Path

_home: Path | None = None
_hosted_homes: frozenset[Path] = frozenset()


def activate_profiles(homes) -> None:
    """Pin the complete owned set before a shared host starts serving profiles."""
    global _hosted_homes
    resolved = frozenset(Path(home).resolve() for home in homes)
    if len(resolved) < 2 or any(not home.is_dir() for home in resolved):
        raise ValueError("A shared host requires distinct existing profile homes")
    if _home is not None or (_hosted_homes and _hosted_homes != resolved):
        raise RuntimeError("Managed runtime ownership is already configured")
    _hosted_homes = resolved


def activate(home: Path) -> None:
    global _home
    resolved = home.resolve()
    if _hosted_homes:
        if resolved not in _hosted_homes:
            raise RuntimeError("The profile is not owned by this shared host")
        return
    if _home is not None and _home != resolved:
        raise RuntimeError("A managed process owns exactly one AgentReady home")
    _home = resolved


def shared_host() -> bool:
    return bool(_hosted_homes)


def enabled() -> bool:
    from hermes_constants import get_hermes_home

    return owns_home(get_hermes_home())


def owns_home(home) -> bool:
    """Whether an explicit runtime home belongs to this managed process."""
    active = Path(home).resolve()
    return active in _hosted_homes if _hosted_homes else active == _home


def managed_home() -> Path:
    if _hosted_homes:
        from hermes_constants import get_hermes_home

        active = Path(get_hermes_home()).resolve()
        if active not in _hosted_homes:
            raise RuntimeError("No owned managed profile is bound to this activity")
        return active
    if _home is None:
        raise RuntimeError("AgentReady runtime has not been activated")
    return _home
