"""Machine-specific state excluded from portable home transfers."""
from __future__ import annotations

from pathlib import Path


# Dependency generations, downloaded tools, and partial transfers belong to this machine.
PM_RUNTIME_ROOT_DIRS = frozenset({"installs", "tools", "cache"})


def profile_root_entry(parts: tuple[str, ...]) -> str | None:
    """Locate a home-root entry without excluding same-named skill/plugin data."""
    if len(parts) >= 3 and parts[0] == "profiles":
        return parts[2]
    return parts[0] if parts else None


def is_subscription_binding(parts: tuple[str, ...], *, full_home: bool = False) -> bool:
    """The non-portable AGE-11 subtree, relative to one profile or a full Hermes home."""
    folded = tuple(part.casefold() for part in parts)
    if full_home and len(folded) >= 4 and folded[0] == "profiles":
        folded = folded[2:]
    return folded[:2] == ("subscriptions", "agy")


def subscription_binding_entries(root: Path, directory: str, names: list[str]) -> set[str]:
    relative = Path(directory).relative_to(root).parts
    return {name for name in names if is_subscription_binding((*relative, name))}
