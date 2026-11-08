"""The platform login skill resolves directly from the common image, without copying homes."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent import skill_utils as su
from agent import prompt_builder as pb
from tools import skills_tool as st
from tools import skill_manager_tool as sm
from tools import skill_usage as usage
from tools import skills_sync as sync

NAME = 'agentsee-google-browser-login'
ASSET = Path(__file__).resolve().parents[2] / 'skills/agentsee' / NAME / 'SKILL.md'


def write_skill(root, name, body='Personal skill.'):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: A personal skill.\n---\n\n{body}\n')
    return folder


@pytest.fixture
def library(tmp_path, monkeypatch):
    home, bundle = tmp_path / 'home', tmp_path / 'packaged'
    home.mkdir()
    system = bundle / 'agentsee' / NAME
    system.mkdir(parents=True)
    shutil.copyfile(ASSET, system / 'SKILL.md')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_BUNDLED_SKILLS', str(bundle))
    monkeypatch.setattr(su, 'get_system_skills_dir', lambda: bundle / 'agentsee')
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home))
    monkeypatch.setattr(st, 'SKILLS_DIR', home / 'skills')
    monkeypatch.setattr(sm, 'SKILLS_DIR', home / 'skills')
    monkeypatch.setattr(su, 'get_project_skills_dirs', list)
    st._SKILLS_CACHE.clear()
    try:
        yield home, bundle, system
    finally:
        reset_hermes_home_override(token)


def test_skill_metadata_and_native_tool_contract():
    fm, body = su.parse_frontmatter(ASSET.read_text())
    assert fm['name'] == NAME and fm['author'].startswith('Mikhail Arlashkin,')
    assert len(fm['description']) <= 60 and fm['description'].endswith('.')
    assert fm['platforms'] == ['linux', 'macos', 'windows']
    assert '`browser_exec`' in body and '`keep_tab()`' in body
    headings = ['When to Use', 'Prerequisites', 'How to Run', 'Quick Reference', 'Procedure', 'Pitfalls', 'Verification']
    assert [body.index('## ' + x) for x in headings] == sorted(body.index('## ' + x) for x in headings)
    assert 'parent/coordinator' in body
    assert 'Настройки сотрудника → Google в браузере сотрудника → Войти в Google' in body
    assert '**Готово**' in body and 'does not verify Google authentication' in body
    assert 'do not substitute a full Desktop view' in body
    assert not fm.get('required_environment_variables') and not fm.get('deps')


def test_fresh_home_list_view_index_and_preload_are_source_owned(library):
    home, _bundle, system = library
    assert not (home / 'skills').exists()
    prompt = pb.build_skills_system_prompt(skills_dir_override=home / 'skills')
    listed = json.loads(st.skills_list(category='agentsee'))
    viewed = json.loads(st.skill_view(NAME, preprocess=False))
    assert listed.get('count') == 1 and listed['skills'][0]['name'] == NAME, (listed, su.__file__)
    assert NAME in prompt and 'agentsee' in prompt
    assert viewed['success'] and viewed['content'] == (system / 'SKILL.md').read_text()
    assert Path(viewed['skill_dir']) == system
    from agent.skill_commands import build_preloaded_skills_prompt
    text, loaded, missing = build_preloaded_skills_prompt([NAME])
    assert loaded == [NAME] and not missing and 'Use protected fill or a human handoff' in text
    assert not list((home / 'skills').glob('**/SKILL.md'))


def test_modified_profile_project_external_copies_preserved_but_system_wins(library, monkeypatch):
    home, _bundle, system = library
    roots = [home / 'skills', home / 'project-skills', home / 'external']
    copies = [write_skill(root, NAME, 'Private changed copy.') / 'SKILL.md' for root in roots]
    before = {p: p.read_bytes() for p in copies}
    monkeypatch.setattr(su, 'get_project_skills_dirs', lambda: [roots[1]])
    monkeypatch.setattr(su, 'get_external_skills_dirs', lambda: [roots[2]])
    result = json.loads(st.skill_view(NAME, preprocess=False))
    assert result['success'] and result['content'] == (system / 'SKILL.md').read_text()
    rows = [r for r in json.loads(st.skills_list())['skills'] if r['name'] == NAME]
    assert len(rows) == 1 and rows[0]['category'] == 'agentsee'
    assert all(p.read_bytes() == original for p, original in before.items())


def test_suppression_optout_and_disabled_only_leave_platform_skill_required(library):
    home, bundle, _system = library
    local = home / 'skills'
    local.mkdir()
    (home / '.no-bundled-skills').touch()
    write_skill(bundle / 'web', 'opted-out-bundle')
    (local / '.curator_suppressed').write_text(NAME + '\n')
    (home / 'config.yaml').write_text(f'skills:\n  disabled: [{NAME}, private-disabled]\n  platform_disabled:\n    telegram: [{NAME}]\n')
    write_skill(local, 'private-disabled')
    sync.sync_skills(quiet=True)
    assert not (local / 'web/opted-out-bundle').exists()
    assert not (local / 'agentsee').exists()
    assert NAME not in su.get_disabled_skill_names('telegram')
    assert 'private-disabled' in su.get_disabled_skill_names('telegram')
    assert json.loads(st.skill_view(NAME, preprocess=False))['success']
    assert not json.loads(st.skill_view('private-disabled', preprocess=False))['success']
    assert [r['name'] for r in json.loads(st.skills_list())['skills']] == [NAME]
    from agent.skill_commands import build_preloaded_skills_prompt
    _text, loaded, missing = build_preloaded_skills_prompt([NAME, 'private-disabled'])
    assert loaded == [NAME] and missing == ['private-disabled']
    assert NAME in pb.build_skills_system_prompt(skills_dir_override=local)


def test_unrelated_precedence_stays_project_local_create_external(library, monkeypatch):
    home, _bundle, _system = library
    roots = [home / 'project', home / 'skills', home / 'create', home / 'external']
    for idx, root in enumerate(roots):
        write_skill(root, 'ordinary', f'Layer {idx}.')
    monkeypatch.setattr(su, 'get_project_skills_dirs', lambda: [roots[0]])
    monkeypatch.setattr(su, 'get_skill_create_dir', lambda: roots[2])
    monkeypatch.setattr(su, 'get_external_skills_dirs', lambda: [roots[3]])
    assert [tier for tier, root in su.get_skill_search_roots()] == [-1, 0, 1, 2, 3]
    for idx, root in enumerate(roots):
        assert f'Layer {idx}.' in json.loads(st.skill_view('ordinary', preprocess=False))['content']
        shutil.rmtree(root / 'ordinary')


@pytest.mark.parametrize('action,kwargs', [
    ('create', {'content': 'unused'}), ('edit', {'content': 'unused'}),
    ('patch', {'old_string': 'Sign in', 'new_string': 'Changed'}),
    ('delete', {}), ('write_file', {'file_path': 'notes.md', 'file_content': 'changed'}),
    ('remove_file', {'file_path': 'notes.md'}),
])
def test_manager_refuses_all_system_mutations_before_gate(library, monkeypatch, action, kwargs):
    _home, _bundle, system = library
    before = (system / 'SKILL.md').read_bytes()
    monkeypatch.setattr(sm, '_run_write_gate', lambda *_args: pytest.fail('system write reached staging'))
    result = json.loads(sm.skill_manage(action, NAME, **kwargs))
    assert not result['success'] and 'read-only' in result['error']
    assert (system / 'SKILL.md').read_bytes() == before
    assert not (system / 'notes.md').exists()


def test_batch_refused_before_snapshot_or_rollback(library, monkeypatch):
    home, _bundle, system = library
    personal = write_skill(home / 'skills', 'personal', 'Keep this.')
    before = {p: p.read_bytes() for p in [system / 'SKILL.md', personal / 'SKILL.md']}
    from tools import skill_manager_batch
    monkeypatch.setattr(skill_manager_batch, '_snapshot_skills', lambda *_args: pytest.fail('snapshot reached'))
    result = json.loads(sm.skill_manage('', '', operations=[
        {'action': 'patch', 'name': 'personal', 'old_string': 'Keep this.', 'new_string': 'Changed.'},
        {'action': 'write_file', 'name': NAME, 'file_path': 'notes.md', 'file_content': 'Bad.'},
    ]))
    assert not result['success'] and 'read-only' in result['error']
    assert all(p.read_bytes() == data for p, data in before.items())


def test_curator_cannot_adopt_or_archive_system_or_shadow_copy(library):
    home, _bundle, system = library
    personal = write_skill(home / 'skills', NAME, 'Private changed copy.')
    before = {p: p.read_bytes() for p in [system / 'SKILL.md', personal / 'SKILL.md']}
    assert usage.is_protected_builtin(NAME)
    assert not usage.is_curation_eligible(NAME, system)
    assert not usage.adopt_skill(NAME)[0]
    assert not usage.archive_skill(NAME)[0]
    assert all(p.read_bytes() == data for p, data in before.items())


def test_bundle_sync_does_not_copy_system_namespace_or_remove_private_shadow(library):
    home, bundle, _system = library
    ordinary = write_skill(bundle / 'web', 'ordinary-bundled')
    personal = write_skill(home / 'skills', NAME, 'Preserved.') / 'SKILL.md'
    before = personal.read_bytes()
    assert sync._discover_bundled_skills(bundle) == [('ordinary-bundled', ordinary)]
    sync.sync_skills(quiet=True)
    assert personal.read_bytes() == before
    assert not (home / 'skills/agentsee').exists()
    assert (home / 'skills/web/ordinary-bundled/SKILL.md').exists()


def test_source_escape_is_neither_indexed_nor_viewed_and_normalization_is_shared(library, tmp_path):
    _home, bundle, system = library
    outside = write_skill(tmp_path / 'outside', 'foreign')
    link = bundle / 'agentsee/foreign'
    link.symlink_to(outside, target_is_directory=True)
    assert not su.is_system_skill_path(link / 'SKILL.md')
    assert 'foreign' not in [r['name'] for r in json.loads(st.skills_list())['skills']]
    assert not json.loads(st.skill_view('foreign', preprocess=False))['success']
    assert su.normalize_skill_lookup_name(str(system / 'SKILL.md')) == NAME
    from agent.skill_commands import build_preloaded_skills_prompt
    assert json.loads(st.skill_view(su.normalize_skill_lookup_name(str(system / 'SKILL.md')), preprocess=False))['success']
    _text, loaded, missing = build_preloaded_skills_prompt([str(system / 'SKILL.md')])
    assert loaded == [NAME] and not missing


def test_curator_external_provenance_is_not_shifted_by_system_root(library, monkeypatch):
    home, _bundle, _system = library
    write_skill(home / 'skills', 'personal')
    external = write_skill(home / 'external', 'external')
    monkeypatch.setattr(su, 'get_external_skills_dirs', lambda: [external.parent])
    assert usage._find_external_skill_dir('personal') is None
    assert usage._find_external_skill_dir('external') == external
    assert usage._external_skill_names() == {'external'}


def test_restart_without_bootstrap_reuses_authoritative_source(library):
    home, bundle, _system = library
    # Two fresh interpreters model a full gateway restart; no seeding, model or browser call.
    script = "import json;from tools.skills_tool import skill_view; p=json.loads(skill_view('" + NAME + "',preprocess=False));print(json.dumps({'success':p['success'],'skillDir':p['skill_dir'],'content':p['content']}))"
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_BUNDLED_SKILLS': str(bundle)}
    for _ in range(2):
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        assert payload == {'success': True, 'skillDir': str(ASSET.parent), 'content': ASSET.read_text()}


@pytest.mark.parametrize('symlink', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_create_dir_cannot_target_system_library_before_mkdir_or_snapshot(library, tmp_path, monkeypatch, symlink, batch):
    _home, bundle, _system = library
    target = bundle / 'agentsee'
    if symlink:
        target = tmp_path / 'system-link'
        target.symlink_to(bundle / 'agentsee', target_is_directory=True)
    monkeypatch.setattr(su, 'get_skill_create_dir', lambda: target)
    from tools import skill_manager_batch
    monkeypatch.setattr(skill_manager_batch, '_snapshot_skills', lambda *_args: pytest.fail('system creation reached snapshot'))
    monkeypatch.setattr(sm, '_run_write_gate', lambda *_args: pytest.fail('system creation reached gate'))
    content = '---\nname: new-name\ndescription: A new skill.\n---\n\nNo writes.\n'
    if batch:
        result = json.loads(sm.skill_manage('', '', operations=[{'action': 'create', 'name': 'new-name', 'content': content}]))
    else:
        result = json.loads(sm.skill_manage('create', 'new-name', content=content))
    assert not result['success'] and 'read-only' in result['error']
    assert not (target / 'new-name').exists()
    assert not sm._create_skill('new-name', content)['success']
    assert not (target / 'new-name').exists()


def test_curator_cannot_mutate_source_path_through_noncanonical_alias(library):
    _home, _bundle, system = library
    assert not usage.is_curation_eligible('different-name', system)




def test_current_source_asset_wins_after_upgrade_without_home_migration(library):
    home, _bundle, system = library
    personal = write_skill(home / 'skills', NAME, 'Old private overlay.') / 'SKILL.md'
    before = personal.read_bytes()
    first = pb.build_skills_system_prompt(skills_dir_override=home / 'skills')
    path = system / 'SKILL.md'
    updated = path.read_text().replace('Sign in to Google in the shared browser with the user.', 'Use Google account sign-in with the user.')
    path.write_text(updated)
    second = pb.build_skills_system_prompt(skills_dir_override=home / 'skills')
    assert 'Use Google account sign-in with the user.' not in first
    assert 'Use Google account sign-in with the user.' in second
    assert json.loads(st.skill_view(NAME, preprocess=False))['content'] == updated
    assert personal.read_bytes() == before


def test_shared_system_library_does_not_mix_two_profiles_personal_skills(library, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home, _bundle, system = library
    second = home.parent / 'legacy-owner-home'
    second.mkdir()
    write_skill(home / 'skills', 'only-first')
    write_skill(second / 'skills', 'only-legacy-owner')
    monkeypatch.setattr(st, 'SKILLS_DIR', st._SKILLS_DIR_AT_IMPORT)
    for own_home, own_name, foreign_name in [(home, 'only-first', 'only-legacy-owner'), (second, 'only-legacy-owner', 'only-first')]:
        token = set_hermes_home_override(str(own_home))
        try:
            names = {r['name'] for r in json.loads(st.skills_list())['skills']}
            assert names == {NAME, own_name} and foreign_name not in names
            assert Path(json.loads(st.skill_view(NAME, preprocess=False))['skill_dir']) == system
        finally:
            reset_hermes_home_override(token)


def test_system_authority_ignores_personal_bundled_environment(tmp_path, monkeypatch):
    source = tmp_path / 'installed' / 'agent' / 'skill_utils.py'
    monkeypatch.setattr(su, '__file__', str(source))
    monkeypatch.setenv('HERMES_BUNDLED_SKILLS', str(tmp_path / 'untrusted-personal'))
    assert su.get_system_skills_dir() == source.parent.parent / 'skills' / 'agentsee'


def test_new_destination_and_outgoing_symlink_in_source_namespace_are_protected(library, tmp_path):
    _home, bundle, _system = library
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    alias = bundle / 'agentsee' / 'escaped'
    alias.symlink_to(foreign, target_is_directory=True)
    assert su.is_protected_system_skill_path(alias / 'new-name')
    assert not su.is_system_skill_path(alias / 'SKILL.md')
    assert su.is_protected_system_skill_path(bundle / 'agentsee' / 'not-yet-created')
