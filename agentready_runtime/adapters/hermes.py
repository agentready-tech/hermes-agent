"""Explicit process entry and profile gate for the managed Hermes adapter."""

from pathlib import Path

_home: Path | None = None


def activate(home: Path) -> None:
    global _home
    resolved = home.resolve()
    if _home is not None and _home != resolved:
        raise RuntimeError("A managed process owns exactly one AgentReady home")
    _home = resolved


def enabled() -> bool:
    if _home is None:
        return False
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()).resolve() == _home


def managed_home() -> Path:
    if _home is None:
        raise RuntimeError("AgentReady runtime has not been activated")
    return _home
