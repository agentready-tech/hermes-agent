"""Profile-owned builtin selection reads image assets without copying a library package."""
import json
import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from tests.agent import test_managed_skill_library as managed_fixtures
from tests.agent.test_managed_skill_library import own_skill, publish, skill, viewed

home = managed_fixtures.home


def test_positive_profile_selection_exposes_original_builtin_after_library_publish(home):
    from hermes_runtime.standalone.config import read_config
    publish(home, [skill()])
    config = read_config(home)
    config['skills']['builtin_enabled'] = ['github']
    (home / 'config.yaml').write_text(json.dumps(config))
    result = viewed('github')
    assert 'error' not in result, 'explicit profile opt-in must expose the installed original'


@pytest.fixture
def assets(home, tmp_path, monkeypatch):
    from agent import builtin_skills
    roots = (tmp_path / 'installed' / 'skills', tmp_path / 'installed' / 'optional-skills')
    main = own_skill(roots[0], 'github', 'Native fixture procedure.')
    (main.parent / 'references').mkdir()
    (main.parent / 'references' / 'guide.md').write_text('Native fixture resource.')
    own_skill(roots[1], 'future-native', 'Future fixture procedure.')
    monkeypatch.setattr(builtin_skills, 'installed_roots', lambda: roots)
    publish(home, [skill()])
    return roots


def test_selected_builtin_uses_original_resources_and_preserves_tiers_config_and_home(home, assets, tmp_path):
    from hermes_runtime.builtin_skills import builtin_inventory, builtin_select
    before = (home / 'config.yaml').read_text()
    initial = builtin_inventory(home)
    from agent.prompt_builder import build_skills_system_prompt
    before_prompt = build_skills_system_prompt()
    assert initial['enabled'] == [] and {r['name'] for r in initial['skills']} == {'github', 'future-native'}
    assert all(not r['enabled'] for r in initial['skills'])
    request = {'enabled': ['github'], 'expectedRevision': initial['revision']}
    result = builtin_select(home, request)
    assert result['enabled'] == ['github']
    assert next(r for r in result['skills'] if r['name'] == 'github')['effective']
    assert 'Native fixture procedure.' in viewed('github')['content']
    from agent.skills_index_delta import stage_skills_index_note
    agent = SimpleNamespace(valid_tool_names={'skills_list', 'skill_view'}, platform='cli', provider='custom',
                            api_mode='chat_completions', session_id='fixture',
                            _session_db=SimpleNamespace(db_path=str(home / 'state.db')))
    messages = [{'role': 'system', 'content': before_prompt}]
    assert stage_skills_index_note(agent, before_prompt, messages)
    assert 'github' in agent._skills_index_note and messages == [{'role': 'system', 'content': before_prompt}]
    from tools.skills_tool import skill_view
    assert 'Native fixture resource.' in json.loads(skill_view('github', file_path='references/guide.md', preprocess=False))['content']
    disabled = builtin_select(home, {'enabled': [], 'expectedRevision': result['revision']})
    assert 'error' in viewed('github')
    assert 'error' in json.loads(skill_view('github', file_path='references/guide.md', preprocess=False))
    restored = builtin_select(home, {'enabled': ['github'], 'expectedRevision': disabled['revision']})
    assert restored['enabled'] == ['github']
    assert 'Native fixture procedure.' in viewed('github')['content']
    assert 'Native fixture resource.' in json.loads(skill_view('github', file_path='references/guide.md', preprocess=False))['content']
    assert 'error' in viewed('future-native')
    assert not (home / 'skills' / 'github').exists()
    from agent.skill_utils import is_protected_system_skill_path
    assert is_protected_system_skill_path(assets[0] / 'github' / 'SKILL.md')
    from agent.skill_library import custody_root
    assert not any(custody_root(home).glob('releases/*/optional/github'))
    from hermes_runtime.standalone.config import read_config
    assert read_config(home)['skills']['disabled'] == ['required-example']
    assert 'environment_id' in before and read_config(home)['autonomous']['panel']['environment_id']
    own_skill(home / 'skills', 'github', 'Personal fixture wins.')
    assert 'Personal fixture wins.' in viewed('github')['content']
    assert next(r for r in builtin_inventory(home)['skills'] if r['name'] == 'github')['shadowed']
    (home / 'skills' / 'github' / 'SKILL.md').unlink()
    publish(home, [skill(), skill('github', 'optional', 'Managed optional wins.')], ['github'], revision=2)
    assert 'Managed optional wins.' in viewed('github')['content']
    other = tmp_path / 'other-profile'
    other.mkdir()
    publish(other, [skill()])
    assert builtin_inventory(other)['enabled'] == []
    assert builtin_inventory(home)['enabled'] == ['github']
    assert builtin_select(home, request)['enabled'] == ['github'], 'exact retry is idempotent'


def test_builtin_selection_cas_unknown_and_policy_metadata_never_drop_or_reenable(home, assets, monkeypatch):
    from hermes_runtime.builtin_skills import builtin_inventory, builtin_select
    from hermes_runtime.skill_inventory import SkillReadError
    first = builtin_inventory(home)
    selected = builtin_select(home, {'enabled': ['github'], 'expectedRevision': first['revision']})
    with pytest.raises(SkillReadError, match='CONFLICT_BUILTIN_SKILLS_CHANGED'):
        builtin_select(home, {'enabled': ['future-native'], 'expectedRevision': first['revision']})
    from hermes_runtime.standalone.config import read_config
    config = read_config(home)
    config['skills'].update(builtin_enabled=['github', 'removed-native'], disabled=['github'],
                            platform_disabled={'telegram': ['future-native']})
    (home / 'config.yaml').write_text(json.dumps(config))
    assert next(r for r in builtin_inventory(home)['skills'] if r['name'] == 'future-native')['blockedReason'] is None
    monkeypatch.setenv('HERMES_PLATFORM', 'telegram')
    inventory = builtin_inventory(home)
    rows = {r['name']: r for r in inventory['skills']}
    assert rows['removed-native']['enabled'] and rows['removed-native']['blockedReason'] == 'unavailable'
    assert rows['github']['blockedReason'] == 'disabled' and not rows['github']['effective']
    assert rows['future-native']['blockedReason'] == 'platform-disabled'
    with pytest.raises(SkillReadError, match='INVALID_SKILLS_BUILTIN_BLOCKED'):
        builtin_select(home, {'enabled': ['future-native', 'github', 'removed-native'],
                              'expectedRevision': inventory['revision']})
    with pytest.raises(SkillReadError, match='INVALID_SKILLS_BUILTIN_UNAVAILABLE'):
        builtin_select(home, {'enabled': ['github', 'missing'], 'expectedRevision': inventory['revision']})
    assert builtin_inventory(home)['enabled'] == ['github', 'removed-native']
    assert selected['revision'] != first['revision']


def test_existing_native_http_and_panel_channel_bind_owner_cas_and_readonly(home, assets):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.skills import router
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_runtime.standalone.panel_skills import SkillsRead
    from hermes_runtime.skill_inventory import SkillReadError
    app = FastAPI()
    app.include_router(router)
    grant = {'purpose': 'native-http', 'capabilities': frozenset({'http:owner'}), 'profile_id': str(home)}

    @app.middleware('http')
    async def principal(request, call_next):
        request.state.native_http_principal = grant
        token = set_hermes_home_override(home)
        try:
            return await call_next(request)
        finally:
            reset_hermes_home_override(token)

    with TestClient(app) as client:
        calls = []
        def native(path, payload=None, *, method):
            calls.append((method, path))
            response = client.request(method, path, **({'json': payload} if payload is not None else {}))
            if response.status_code >= 400:
                raise HTTPError('native', response.status_code, 'typed', {}, BytesIO(response.content))
            return response.json()
        auth = SimpleNamespace(home=home.resolve(), gateway_owned=True, endpoint=lambda: None,
                               authenticated_request=native)
        reader = SkillsRead(home, auth)
        inventory = asyncio.run(reader.handle('skills.builtin.list', {}))
        assert reader.builtin_available()
        request = {'enabled': ['github'], 'expectedRevision': inventory['revision']}
        count = len(calls)
        with pytest.raises(SkillReadError, match='FORBIDDEN'):
            asyncio.run(reader.handle('skills.builtin.select', request, can_edit_personal=False))
        assert len(calls) == count
        result = asyncio.run(reader.handle('skills.builtin.select', request, can_edit_personal=True))
        assert result['enabled'] == ['github']
        stale = client.post('/api/skills/library/builtin/selection',
                            json={'enabled': ['future-native'], 'expectedRevision': inventory['revision']})
        assert stale.status_code == 409 and stale.json()['detail'] == 'CONFLICT_BUILTIN_SKILLS_CHANGED'
        assert client.get('/api/skills/library/builtin?profile=foreign').status_code == 400
        assert client.post('/api/skills/library/builtin/selection', content=b'x' * 16385).status_code == 413
        assert asyncio.run(reader.handle('skills.library.list', {}))['capability'] == 'skills-library-read-v1'
        grant['profile_id'] = str(home.parent / 'foreign')
        assert client.get('/api/skills/library/builtin').status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('builtin_ready', [False, True])
async def test_hello_advertises_builtin_only_after_native_capability_probe(monkeypatch, builtin_ready):
    from hermes_runtime.standalone.panel import connection
    sent = []
    class Control:
        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield Control()

    monkeypatch.setattr('websockets.asyncio.client.connect', connect)
    skills = SimpleNamespace(available=lambda: True, builtin_available=lambda: builtin_ready)
    await connection({'url': 'https://panel.invalid', 'environment_id': 'env_' + 'a' * 32},
                     0, 'fixture', None, skills=skills)
    assert ('skills-builtin-selection-v1' in sent[0]['capabilities']) == builtin_ready
    assert 'skills-library-read-v1' in sent[0]['capabilities']
