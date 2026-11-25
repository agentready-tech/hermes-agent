"""Native profile config transactions and comment-preserving publication."""
from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Any, Optional


def _omitted_config_paths(
    existing: dict[str, Any], proposed: dict[str, Any], prefix: tuple[str, ...] = (),
) -> list[str]:
    """Mapping paths that *proposed* would delete by omission from *existing*.

    The round-trip writer recurses through mappings, so the completeness check must recurse too:
    checking top-level key counts still lets a partial ``plugins: {...}`` payload erase sibling
    settings inside that section. Replacing a non-empty mapping with a scalar/list is likewise a
    deletion of that mapping's children and is reported at the mapping path.
    """
    omitted: list[str] = []
    for key, old_value in existing.items():
        path = (*prefix, str(key))
        if key not in proposed:
            omitted.append(".".join(path))
            continue
        new_value = proposed[key]
        if isinstance(old_value, dict):
            if isinstance(new_value, dict):
                omitted.extend(_omitted_config_paths(old_value, new_value, path))
            elif old_value:
                omitted.append(".".join(path))
    return omitted


_CONFIG_FILE_HOLDERS = threading.local()


@contextmanager
def config_write_lock(config_path: Path):
    """Serialize a native config mutation/readback by physical profile, across processes.

    Hold across raw read, mutation and atomic replacement. It is not an inference lock.
    The existing process lock comes first, including for save_config/plugin callers.
    """
    from hermes_cli.auth import _file_lock
    from hermes_cli.config import _CONFIG_LOCK

    path = Path(config_path).resolve()
    holders = vars(_CONFIG_FILE_HOLDERS)
    key = str(path)
    if key in holders:
        yield
        return
    with _CONFIG_LOCK, _file_lock(path.with_name(path.name + ".lock"), threading.local(),
                                  5.0, "Timed out waiting for the profile config writer"):
        holders[key] = True
        try:
            yield
        finally:
            del holders[key]


def _write_config_state(
    config_path: Path, data: dict[str, Any], *, allow_omissions: bool,
    extra_content_on_create: Optional[str] = None,
) -> None:
    """Shared comment-preserving config writer; omission policy is selected by the public wrapper."""
    from hermes_cli.config import _refuse_failed_read, require_readable_config_before_write, _refuse_overwrite
    from utils import atomic_roundtrip_yaml_save

    with config_write_lock(config_path):
        _refuse_failed_read(config_path, data)
        if not allow_omissions:
            existing = require_readable_config_before_write(config_path)
            omitted = _omitted_config_paths(existing, data)
            if omitted:
                shown = ", ".join(omitted[:12])
                if len(omitted) > 12:
                    shown += f", +{len(omitted) - 12} more"
                exc = ValueError(f"omitted config paths: {shown}")
                raise _refuse_overwrite(
                    config_path,
                    "would lose settings omitted by this write",
                    exc,
                    "Pass the complete current config, or use atomic_config_replace() only when "
                    "deletion by omission is deliberate.",
                ) from exc
        atomic_roundtrip_yaml_save(config_path, data, extra_content_on_create=extra_content_on_create)


def atomic_config_write(
    config_path: Path, data: dict[str, Any], *, extra_content_on_create: Optional[str] = None,
) -> None:
    """Persist config without allowing an incomplete mapping to delete existing settings.

    Values explicitly present in *data* may change, but every existing mapping path must remain.
    Use ``atomic_config_replace`` for a deliberate full-state replacement where omitted keys are
    meant to be deleted. Both paths retain the unreadable-file guard and ruamel comment preservation.
    """
    _write_config_state(
        config_path, data, allow_omissions=False, extra_content_on_create=extra_content_on_create)


def atomic_config_replace(
    config_path: Path, data: dict[str, Any], *, extra_content_on_create: Optional[str] = None,
) -> None:
    """Persist the complete desired config state; omitted mapping keys are deliberately deleted."""
    _write_config_state(
        config_path, data, allow_omissions=True, extra_content_on_create=extra_content_on_create)
