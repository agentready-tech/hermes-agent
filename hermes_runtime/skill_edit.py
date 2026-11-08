"""Bounded user-authorized OWN skill edits through the owning native gateway."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from agent.skill_library import library_scope
from hermes_runtime.skill_inventory import (
    MAX_FILE_BYTES, MAX_RESPONSE_BYTES, SkillReadError, _bound_home, _catalog, _read_text,
    _safe_path, canonical_name,
)

EDIT_CAPABILITY = "skills-personal-edit-v1"
UPDATE_METHOD = "skills.library.update"
MAX_UPDATE_REQUEST_BYTES = 512 * 1024


def validate_update_request(value):
    if not isinstance(value, dict) or set(value) != {"name", "content", "expectedSha256"}:
        raise SkillReadError("INVALID_SKILLS_REQUEST")
    name = canonical_name(value["name"])
    content, expected = value["content"], value["expectedSha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise SkillReadError("INVALID_SKILLS_EXPECTED_HASH")
    if not isinstance(content, str) or "\x00" in content:
        raise SkillReadError("INVALID_SKILLS_TEXT")
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        raise SkillReadError("INVALID_SKILLS_TEXT") from None
    if len(raw) > MAX_FILE_BYTES:
        raise SkillReadError("INVALID_SKILLS_RESOURCE_TOO_LARGE")
    return name, content, expected


def _own_row(home, name):
    from agent.skill_utils import get_system_skill_names, is_protected_system_skill_path
    rows, truncated = _catalog(home)
    if truncated:
        raise SkillReadError("INVALID_SKILLS_INVENTORY_TRUNCATED")
    matches = [row for row in rows if row["public"]["name"] == name]
    if not matches:
        raise SkillReadError("NOT_FOUND")
    if len(matches) != 1:
        raise SkillReadError("INVALID_SKILLS_AMBIGUOUS_NAME")
    row = matches[0]
    if name in get_system_skill_names() or is_protected_system_skill_path(row["path"]):
        raise SkillReadError("FORBIDDEN")
    return row


def _prospective_response(row, name, content):
    from agent.skill_utils import parse_frontmatter
    from tools.skill_manager_tool import _validate_frontmatter
    if _validate_frontmatter(content):
        raise SkillReadError("INVALID_SKILLS_FRONTMATTER")
    metadata, _body = parse_frontmatter(content)
    if metadata.get("name") != name or not isinstance(metadata.get("description"), str):
        raise SkillReadError("INVALID_SKILLS_NAME_RENAME")
    raw = content.encode("utf-8")
    public = {**row["public"], "description": metadata["description"][:1024],
              "version": metadata.get("version") if isinstance(metadata.get("version"), str) else None}
    result = {"capability": EDIT_CAPABILITY, "skill": public, "content": content,
              "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    # Reserve the existing bounded RPC envelope before any filesystem mutation.
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 1024:
        raise SkillReadError("INVALID_SKILLS_RESPONSE_TOO_LARGE")
    return result


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode,
            info.st_uid, info.st_gid, info.st_nlink)


def _replace_own(path, home, content, expected):
    """The agent skill mutation lock is held; directory descriptors prevent a linked-path redirect."""
    _safe_path(path, home)
    before = path.lstat()
    _text, raw = _read_text(path, home)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SkillReadError("CONFLICT_SKILL_CHANGED")
    parent = path.parent
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    temporary = ".skill-edit-" + secrets.token_hex(16)
    try:
        opened_parent = os.fstat(descriptor)
        current_parent = parent.stat()
        if (opened_parent.st_dev, opened_parent.st_ino) != (current_parent.st_dev, current_parent.st_ino):
            raise SkillReadError("CONFLICT_SKILL_CHANGED")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(temporary, flags, stat.S_IMODE(before.st_mode), dir_fd=descriptor)
        with os.fdopen(file_descriptor, "wb") as stream:
            if (os.fstat(stream.fileno()).st_uid, os.fstat(stream.fileno()).st_gid) != (before.st_uid, before.st_gid):
                os.fchown(stream.fileno(), before.st_uid, before.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(before.st_mode))
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path, home)
        if _identity(os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)) != _identity(before):
            raise SkillReadError("CONFLICT_SKILL_CHANGED")
        os.replace(temporary, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    finally:
        try:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        finally:
            os.close(descriptor)


def personal_update(home, value):
    """CAS one current OWN SKILL.md; never a resource, foreign profile or managed package."""
    from tools.skill_manager_tool import _skill_lock_path, _skill_mutation_locks
    from agent.prompt_builder import clear_skills_system_prompt_cache
    from tools.skills_tool import clear_skills_cache
    home = Path(home).resolve()
    name, content, expected = validate_update_request(value)
    try:
        with _bound_home(home), library_scope(home):
            before = _own_row(home, name)
            _prospective_response(before, name, content)
            aliases = {name, before["path"].parent.name}
            for alias in aliases:
                _safe_path(_skill_lock_path(alias), home)
            with _skill_mutation_locks(aliases):
                row = _own_row(home, name)
                if row["path"] != before["path"]:
                    raise SkillReadError("CONFLICT_SKILL_CHANGED")
                result = _prospective_response(row, name, content)
                _replace_own(row["path"], home, content, expected)
            clear_skills_system_prompt_cache(clear_snapshot=True)
            clear_skills_cache()
            return result
    except SkillReadError:
        raise
    except (OSError, ValueError, NotImplementedError):
        raise SkillReadError("INVALID_SKILLS_UPDATE_UNAVAILABLE") from None
