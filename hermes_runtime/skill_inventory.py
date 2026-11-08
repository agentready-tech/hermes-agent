"""Bounded read-only OWN, assigned OPTIONAL and authoritative SYSTEM inventory."""

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

from agent.skill_library import current_library, library_scope, validate_library, validate_selection
from hermes_yaml import YAMLError

MAX_FILE_BYTES = 64 * 1024


class SkillReadError(ValueError):
    """Only closed, non-private error codes cross the authenticated read boundary."""

    def __init__(self, code, _field=None):
        super().__init__(code)


CAPABILITY = "skills-library-read-v1"
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAX_ROWS = 250
MAX_SCAN_DIRS = 2500
MAX_RESPONSE_BYTES = 256 * 1024


def canonical_name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SkillReadError("INVALID_SKILLS_NAME", "name")
    return value


@contextmanager
def _bound_home(home):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(Path(home).resolve())
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _safe_path(path, home):
    path, home = Path(path), Path(home).resolve()
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise SkillReadError("INVALID_SKILLS_RESOURCE") from None
    current = home
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise SkillReadError("INVALID_SKILLS_RESOURCE")
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise SkillReadError("INVALID_SKILLS_RESOURCE")
    if not path.resolve().is_relative_to(home):
        raise SkillReadError("INVALID_SKILLS_RESOURCE")
    return path


def _read_text(path, home, limit=MAX_FILE_BYTES):
    path = _safe_path(path, home)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SkillReadError("INVALID_SKILLS_RESOURCE")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
                raise SkillReadError("INVALID_SKILLS_RESOURCE")
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        raise SkillReadError("NOT_FOUND") from None
    except OSError:
        raise SkillReadError("INVALID_SKILLS_RESOURCE_UNAVAILABLE") from None
    if len(raw) > limit:
        raise SkillReadError("INVALID_SKILLS_RESOURCE_TOO_LARGE")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SkillReadError("INVALID_SKILLS_TEXT") from None
    if "\x00" in text:
        raise SkillReadError("INVALID_SKILLS_TEXT")
    return text, raw


def _personal_roots(home):
    from hermes_runtime.standalone.config import read_config
    from agent.skill_utils import is_protected_system_skill_path
    root = home / "skills"
    roots = [root]
    try:
        skills = read_config(home).get("skills", {})
        configured = skills.get("create_dir") if isinstance(skills, dict) else None
    except (OSError, ValueError, YAMLError):
        raise SkillReadError("INVALID_SKILLS_PERSONAL_CONFIG") from None
    if isinstance(configured, str) and configured:
        candidate = Path(configured)
        candidate = candidate if candidate.is_absolute() else home / candidate
        if (candidate != home and candidate.is_relative_to(home) and candidate not in roots
                and not candidate.is_relative_to(home / "profiles") and not is_protected_system_skill_path(candidate)):
            roots.append(candidate)
    return [_safe_path(path, home) for path in roots if path.exists() or path.is_symlink()]


def _installed_paths(home):
    """A native HubLockFile owning the same file proves installed provenance, not authorship."""
    from tools.skills_hub import HubLockFile
    root = home / "skills"
    lock_path = root / ".hub" / "lock.json"
    if not lock_path.exists():
        return set()
    try:
        _read_text(lock_path, home, 1024 * 1024)
        entries = HubLockFile(path=lock_path).list_installed()
        result = set()
        for entry in entries:
            relative = entry.get("install_path")
            if not isinstance(relative, str) or "\\" in relative:
                continue
            path = _safe_path(root / relative / "SKILL.md", home)
            if path.is_relative_to(root) and path.parent != root:
                result.add(path)
        return result
    except (SkillReadError, ValueError, KeyError, TypeError, AttributeError):
        return set()


def _resources(directory, home):
    from agent.skill_utils import SKILL_SUPPORT_DIRS
    found, truncated, visited = [], False, 0
    for top in sorted(SKILL_SUPPORT_DIRS):
        root = directory / top
        if not root.is_dir() or root.is_symlink():
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > MAX_SCAN_DIRS:
                return found, True
            dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink() and not d.startswith("."))
            for filename in sorted(files):
                path = Path(current) / filename
                if filename.startswith(".") or path.is_symlink():
                    continue
                _safe_path(path, home)
                if len(found) >= 16:
                    truncated = True
                    return found, truncated
                relative = path.relative_to(directory).as_posix()
                if len(relative) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in relative):
                    truncated = True
                    continue
                found.append(relative)
    return found, truncated


def _catalog(home, *, system=False):
    from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS, get_system_skill_names, is_visible_skill_path, parse_frontmatter
    home = Path(home).resolve()
    installed = set() if system else _installed_paths(home)
    common_names = set() if system else get_system_skill_names()
    rows, seen, truncated, visited = [], set(), False, 0
    for root in ([home] if system else _personal_roots(home)):
        for current, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > MAX_SCAN_DIRS:
                truncated = True
                break
            directory = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_SKILL_DIRS
                             and not (directory / d).is_symlink()
                             and not ("SKILL.md" in files and d in SKILL_SUPPORT_DIRS))
            if "SKILL.md" not in files:
                continue
            path = directory / "SKILL.md"
            if not system and not is_visible_skill_path(path):
                continue
            if path in seen:
                continue
            seen.add(path)
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            text, raw = _read_text(path, home)
            metadata, _ = parse_frontmatter(text)
            name = canonical_name(metadata.get("name", directory.name))
            description = metadata.get("description", "")
            if not isinstance(description, str):
                raise SkillReadError("INVALID_SKILLS_FRONTMATTER", name)
            resources, resources_truncated = _resources(directory, home)
            row = {"name": name, "description": description[:1024], "version": metadata.get("version")
                   if isinstance(metadata.get("version"), str) else None, "origin": "system" if system else ("installed" if path in installed else "unknown"),
                   "readOnly": True, "shadowed": name in common_names, "ambiguous": False,
                   "resources": resources, "resourcesTruncated": resources_truncated}
            rows.append({"public": row, "path": path, "content": text, "raw": raw})
        if truncated:
            break
    names = [row["public"]["name"] for row in rows]
    for row in rows:
        row["public"]["ambiguous"] = names.count(row["public"]["name"]) > 1
    return sorted(rows, key=lambda r: r["public"]["name"]), truncated


def personal_inventory(home):
    with _bound_home(home):
        with library_scope(home):
            rows, truncated = _catalog(home)
        payload = {"capability": CAPABILITY, "skills": [], "truncated": truncated}
        for row in rows:
            payload["skills"].append(row["public"])
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 1024:
                payload["skills"].pop()
                payload["truncated"] = True
                break
        return payload


def personal_content(home, name):
    name = canonical_name(name)
    with _bound_home(home):
        with library_scope(home):
            rows, truncated = _catalog(home)
        if truncated:
            raise SkillReadError("INVALID_SKILLS_INVENTORY_TRUNCATED")
        matches = [row for row in rows if row["public"]["name"] == name]
        if not matches:
            raise SkillReadError("NOT_FOUND")
        if len(matches) != 1:
            raise SkillReadError("INVALID_SKILLS_AMBIGUOUS_NAME", name)
        row = matches[0]
        payload = {"capability": CAPABILITY, "skill": row["public"], "content": row["content"],
                   "sha256": hashlib.sha256(row["raw"]).hexdigest(), "bytes": len(row["raw"])}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise SkillReadError("INVALID_SKILLS_RESPONSE_TOO_LARGE")
        return payload



def _managed_catalog(state, kind):
    if state.status not in {"published", "bootstrap"}:
        raise SkillReadError(state.reason or "INVALID_SKILLS_LIBRARY_UNAVAILABLE")
    if kind == "system":
        from agent.skill_utils import get_system_skill_roots
        roots = get_system_skill_roots()
    else:
        roots = state.optional_roots()
    rows, truncated = [], False
    for root in roots:
        found, partial = _catalog(root, system=True)
        for row in found:
            row["public"]["origin"] = kind
        rows.extend(found)
        truncated |= partial
    return rows, truncated


def system_inventory():
    """The owning profile's verified backend mandatory set; image bootstrap only before enrollment."""
    with library_scope() as state:
        rows, truncated = _managed_catalog(state, "system")
        payload = {"capability": CAPABILITY, "skills": [], "truncated": truncated}
        for row in rows:
            payload["skills"].append(row["public"])
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 1024:
                payload["skills"].pop()
                payload["truncated"] = True
                break
        return payload


def system_content(name):
    name = canonical_name(name)
    with library_scope() as state:
        rows, truncated = _managed_catalog(state, "system")
        if truncated:
            raise SkillReadError("INVALID_SKILLS_INVENTORY_TRUNCATED")
        matches = [row for row in rows if row["public"]["name"] == name]
        if not matches:
            raise SkillReadError("NOT_FOUND")
        if len(matches) != 1:
            raise SkillReadError("INVALID_SKILLS_AMBIGUOUS_NAME")
        row = matches[0]
        payload = {"capability": CAPABILITY, "skill": row["public"], "content": row["content"],
                   "sha256": hashlib.sha256(row["raw"]).hexdigest(), "bytes": len(row["raw"])}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise SkillReadError("INVALID_SKILLS_RESPONSE_TOO_LARGE")
        return payload


def validate_control(home, request):
    """Existing protected worker control: validation only, independent from mail enrollment or credentials."""
    from hermes_runtime.standalone.config import settings
    if (not isinstance(request, dict) or set(request) != {"operation", "environmentId", "library", "selectedOptional"}
            or request.get("operation") != "skills_validate"):
        raise SkillReadError("INVALID_SKILLS_OPERATION")
    environment = request["environmentId"]
    if not isinstance(environment, str) or not re.fullmatch(r"env_[a-f0-9]{32}", environment):
        raise SkillReadError("INVALID_SKILLS_ENVIRONMENT")
    config = settings(Path(home))
    panel = config.get("panel", {})
    workspace = config.get("workspace", {})
    if environment not in {panel.get("environment_id"), workspace.get("environment_id")}:
        raise SkillReadError("FORBIDDEN")
    library, _raw = validate_library(request["library"])
    selected = validate_selection(library, request["selectedOptional"])
    return {"capability": "skills-library/v1", "valid": True, "revision": library["revision"], "digest": library["digest"],
            "skillCount": len(library["skills"]), "selectedOptional": list(selected)}
