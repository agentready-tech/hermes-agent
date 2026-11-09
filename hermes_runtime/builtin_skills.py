"""Own-home builtin metadata and configuration selection over the existing native skill channel."""
import hashlib
import json
from pathlib import Path

from agent.builtin_skills import BuiltinSkillsError, MAX_BUILTINS, catalog, selection
from agent.skill_library import library_scope
from hermes_runtime.skill_inventory import MAX_RESPONSE_BYTES, SkillReadError, _bound_home

CAPABILITY = 'skills-builtin-selection-v1'
LIST_METHOD = 'skills.builtin.list'
SELECT_METHOD = 'skills.builtin.select'
CONFLICT = 'CONFLICT_BUILTIN_SKILLS_CHANGED'
REASONS = {None, 'disabled', 'platform-disabled', 'shadowed', 'unavailable'}


def _revision(enabled):
    return hashlib.sha256(json.dumps(list(enabled), separators=(',', ':')).encode()).hexdigest()


def _configured(config):
    skills = config.get('skills') or {}
    if not isinstance(skills, dict):
        raise BuiltinSkillsError('INVALID_SKILLS_BUILTIN_SELECTION')
    return selection(skills.get('builtin_enabled', [])), skills


def _policy(name, disabled, global_disabled, shadowed):
    if name in disabled:
        return 'disabled' if name in global_disabled else 'platform-disabled'
    return 'shadowed' if name in shadowed else None


def _inventory(config):
    from agent.skill_utils import (TIER_BUILTIN, _normalize_string_set, get_disabled_skill_names,
                                  skill_matches_apps, skill_matches_environment, skill_matches_platform)
    from tools.skills_tool import _skill_catalog
    enabled, skills = _configured(config)
    disabled = get_disabled_skill_names()
    global_disabled = _normalize_string_set(skills.get('disabled'))
    assets = {row.name: row for row in catalog()}
    # Match the resolver's aliases too. Ineligible higher-tier owners still own the
    # name (skill_view refuses them rather than silently falling through to a builtin).
    shadowed = {alias for r in _skill_catalog(skip_disabled=True, include_hidden=True) if r['tier'] < TIER_BUILTIN
                for alias in (r['name'], Path(r['path']).parent.name, r['relative_path'])}
    rows = []
    for name in sorted(set(assets) | set(enabled)):
        asset = assets.get(name)
        available = (asset is not None and asset.available and skill_matches_platform(asset.metadata)
                     and skill_matches_environment(asset.metadata) and skill_matches_apps(asset.metadata))
        reason = None if available else 'unavailable'
        reason = reason or _policy(name, disabled, global_disabled, shadowed)
        rows.append({'name': name, 'description': asset.description if asset else '',
                     'version': asset.version if asset else None, 'enabled': name in enabled,
                     'effective': name in enabled and reason is None, 'shadowed': name in shadowed,
                     'blockedReason': reason, 'origin': 'builtin'})
    result = {'capability': CAPABILITY, 'revision': _revision(enabled), 'enabled': list(enabled), 'skills': rows}
    if len(rows) > MAX_BUILTINS or len(json.dumps(result).encode()) > MAX_RESPONSE_BYTES - 1024:
        raise SkillReadError('INVALID_SKILLS_BUILTIN_CATALOG_TOO_LARGE')
    return result


def builtin_inventory(home):
    from hermes_cli.config import read_user_config_raw
    with _bound_home(home), library_scope(home):
        try:
            return _inventory(read_user_config_raw(Path(home) / 'config.yaml'))
        except BuiltinSkillsError as error:
            raise SkillReadError(str(error)) from None


def validate_select(value):
    import re
    if (not isinstance(value, dict) or set(value) != {'enabled', 'expectedRevision'}
            or not isinstance(value['expectedRevision'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', value['expectedRevision'])):
        raise SkillReadError('INVALID_SKILLS_BUILTIN_SELECTION')
    try:
        return selection(value['enabled'])
    except BuiltinSkillsError as error:
        raise SkillReadError(str(error)) from None


def project_inventory(value):
    from hermes_runtime.skill_inventory import canonical_name
    if (not isinstance(value, dict) or set(value) != {'capability', 'revision', 'enabled', 'skills'}
            or value['capability'] != CAPABILITY or not isinstance(value['skills'], list)
            or len(value['skills']) > MAX_BUILTINS):
        raise SkillReadError('INVALID_SKILLS_BUILTIN_UNAVAILABLE')
    enabled = validate_select({'enabled': value['enabled'], 'expectedRevision': value['revision']})
    fields = {'name', 'description', 'version', 'enabled', 'effective', 'shadowed', 'blockedReason', 'origin'}
    names = []
    for row in value['skills']:
        if (not isinstance(row, dict) or set(row) != fields or row['origin'] != 'builtin'
                or not isinstance(row['description'], str) or len(row['description']) > 1024
                or (row['version'] is not None and not isinstance(row['version'], str))
                or any(type(row[k]) is not bool for k in ('enabled', 'effective', 'shadowed'))
                or (row['blockedReason'] is not None and not isinstance(row['blockedReason'], str))
                or row['blockedReason'] not in REASONS):
            raise SkillReadError('INVALID_SKILLS_BUILTIN_UNAVAILABLE')
        name = canonical_name(row['name'])
        if row['enabled'] != (name in enabled) or row['effective'] != (name in enabled and row['blockedReason'] is None):
            raise SkillReadError('INVALID_SKILLS_BUILTIN_UNAVAILABLE')
        names.append(name)
    if names != sorted(set(names)) or not set(enabled).issubset(names) or value['revision'] != _revision(enabled):
        raise SkillReadError('INVALID_SKILLS_BUILTIN_UNAVAILABLE')
    if len(json.dumps(value).encode()) > MAX_RESPONSE_BYTES - 1024:
        raise SkillReadError('INVALID_SKILLS_BUILTIN_CATALOG_TOO_LARGE')
    return value


def builtin_select(home, value):
    from hermes_cli.config import _CONFIG_LOCK, atomic_config_write, read_user_config_raw
    from agent.prompt_builder import clear_skills_system_prompt_cache
    from tools.skills_tool import clear_skills_cache
    requested = validate_select(value)
    with _bound_home(home), library_scope(home), _CONFIG_LOCK:
        try:
            path = Path(home) / 'config.yaml'
            config = read_user_config_raw(path)
            before = _inventory(config)
            if list(requested) == before['enabled']:
                return before
            if value['expectedRevision'] != before['revision']:
                raise SkillReadError(CONFLICT)
            rows = {row['name']: row for row in before['skills']}
            for name in set(requested) - set(before['enabled']):
                if name not in rows or rows[name]['blockedReason'] == 'unavailable':
                    raise SkillReadError('INVALID_SKILLS_BUILTIN_UNAVAILABLE')
                if rows[name]['blockedReason'] in {'disabled', 'platform-disabled'}:
                    raise SkillReadError('INVALID_SKILLS_BUILTIN_BLOCKED')
            config.setdefault('skills', {})['builtin_enabled'] = list(requested)
            atomic_config_write(path, config)
            clear_skills_system_prompt_cache(clear_snapshot=True)
            clear_skills_cache()
            return _inventory(config)
        except BuiltinSkillsError as error:
            raise SkillReadError(str(error)) from None
