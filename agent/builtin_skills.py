"""Explicit profile opt-ins to immutable installed skills; no seeding or library projection."""
from dataclasses import dataclass, field, replace
from functools import lru_cache
import os
from pathlib import Path
import re

MAX_BUILTINS = 250
_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')


class BuiltinSkillsError(ValueError):
    pass


def installed_roots():
    # Unlike updater discovery, this authority never accepts environment redirects.
    installed = Path(__file__).resolve().parent.parent
    return (installed / 'skills', installed / 'optional-skills')


def selection(value):
    if (not isinstance(value, list) or len(value) > MAX_BUILTINS
            or any(not isinstance(n, str) or not _NAME.fullmatch(n) for n in value)
            or value != sorted(set(value))):
        raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_SELECTION')
    return tuple(value)


@dataclass(frozen=True)
class Builtin:
    name: str
    description: str
    version: str | None
    path: Path
    available: bool = True
    metadata: dict = field(default_factory=dict, repr=False, compare=False)


def _package(path, root):
    from agent.skill_utils import parse_frontmatter
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_ASSETS')
    with path.open(encoding='utf-8-sig') as stream:
        metadata, _ = parse_frontmatter(stream.read(64 * 1024))
    name = metadata.get('name', path.parent.name)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_ASSETS')
    description = metadata.get('description', '')
    version = metadata.get('version')
    return Builtin(name, description[:1024] if isinstance(description, str) else '',
                   version if isinstance(version, str) else None, path.parent, metadata=metadata)


@lru_cache(maxsize=8)
def _catalog(roots):
    from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS
    found, visited = {}, 0
    for root in roots:
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_ASSETS')
        for current, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > 2500:
                raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_CATALOG_TOO_LARGE')
            directory = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_SKILL_DIRS
                             and not (directory / d).is_symlink()
                             and not (directory == root and d == 'agentsee')
                             and not ('SKILL.md' in files and d in SKILL_SUPPORT_DIRS))
            if 'SKILL.md' not in files:
                continue
            row = _package(directory / 'SKILL.md', root)
            if row.name in found:
                row = replace(row, available=False)
            found[row.name] = row
            if len(found) > MAX_BUILTINS:
                raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_CATALOG_TOO_LARGE')
    return tuple(found[n] for n in sorted(found))


def catalog():
    try:
        return _catalog(tuple(installed_roots()))
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, BuiltinSkillsError):
            raise
        raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_ASSETS') from None


def selected_roots():
    from agent.skill_utils import _skills_cfg
    try:
        names = selection((_skills_cfg() or {}).get('builtin_enabled', []))
        return [row.path for row in catalog() if row.name in names and row.available]
    except BuiltinSkillsError:
        return []  # a bad opt-in never invalidates personal or managed library packages


def is_selected_path(path):
    target = Path(path).resolve()
    return any(target.is_relative_to(root) for root in selected_roots())


def is_builtin_path(path):
    target = Path(path).resolve()
    return any(target.is_relative_to(root) for root in installed_roots())
