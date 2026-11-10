"""Managed linked resources execute at the advertised directory in the existing shared skills tree."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent import skill_library as library
from tests.agent.test_managed_skill_library import home as _home_fixture
from tests.agent.test_managed_skill_library import own_skill, publish, skill, viewed

pytestmark = pytest.mark.platforms('posix')


@pytest.fixture(name="home")
def resource_home(request):
    return request.getfixturevalue("_home_fixture")


def finance_skill():
    row = skill('finance-ledger', 'optional', 'Use the physical linked resources; keep user data in the workspace.')
    row['files'] += [
        {'path': 'scripts/catalog.py', 'content': 'ITEM = "synthetic-entry"\n'},
        {'path': 'scripts/ledger.py', 'content': 'from pathlib import Path\nimport json, sqlite3\nfrom catalog import ITEM\nfrom reports import result\nfrom schedule import due\ndb=sqlite3.connect(":memory:")\ndb.executescript((Path(__file__).with_name("schema.sql")).read_text())\ndb.execute("insert into ledger values (?)", (ITEM,))\nprint(json.dumps({"result":result(db),"due":due(),"skill_dir":str(Path(__file__).resolve().parent.parent)}))\n'},
        {'path': 'scripts/reports.py', 'content': 'def result(db): return db.execute("select item from ledger").fetchone()[0]\n'},
        {'path': 'scripts/schedule.py', 'content': 'def due(): return "fixture-only"\n'},
        {'path': 'scripts/schema.sql', 'content': 'create table ledger (item text not null);\n'},
    ]
    return row


def test_actual_terminal_executes_all_five_resources_at_advertised_directory(home, tmp_path):
    from agent.skill_utils import is_protected_system_skill_path
    from hermes_runtime.skill_inventory import personal_inventory
    from tools.skills_tool import skills_list
    own_skill(home / 'skills', 'personal-fixture', 'User file remains untouched.')
    marker = home / 'skills/personal-fixture/SKILL.md'
    before = marker.read_bytes()
    publish(home, [skill(), finance_skill()], ['finance-ledger'])
    value = viewed('finance-ledger')
    assert value['success'] is True
    directory = Path(value['skill_dir'])
    assert directory.is_relative_to(home / 'skills')
    assert library.RESOURCE_DIRECTORY in directory.parts
    assert is_protected_system_skill_path(directory) and library.is_library_path(directory)
    assert {path.name for path in (directory / 'scripts').iterdir()} == {'ledger.py', 'catalog.py', 'reports.py', 'schedule.py', 'schema.sql'}
    assert all(path.stat().st_nlink == 1 and path.stat().st_mode & 0o222 == 0 for path in directory.rglob('*') if path.is_file())
    result = subprocess.run([sys.executable, '-B', str(directory / 'scripts/ledger.py')], cwd=tmp_path, capture_output=True, text=True, check=True,
                            env={'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'})
    output = json.loads(result.stdout)
    assert output == {'result': 'synthetic-entry', 'due': 'fixture-only', 'skill_dir': str(directory)}
    assert marker.read_bytes() == before
    assert [row['name'] for row in personal_inventory(home)['skills']] == ['personal-fixture']
    listed = json.loads(skills_list())
    assert sum(row['name'] == 'finance-ledger' for row in listed['skills']) == 1


def test_unselected_and_foreign_profile_cannot_resolve_managed_resource_path(home, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    rows = [skill(), finance_skill()]
    publish(home, rows, ['finance-ledger'])
    selected = Path(viewed('finance-ledger')['skill_dir'])
    other = tmp_path / 'other-profile'
    other.mkdir()
    publish(other, rows)
    token = set_hermes_home_override(other)
    try:
        assert 'error' in viewed('finance-ledger')
        assert not any(path.name == 'finance-ledger' for path in library.resource_projection_root(other).rglob('*'))
        assert library.package_metadata(selected) is None
    finally:
        reset_hermes_home_override(token)


def test_resource_mirror_retains_managed_no_inline_execution_classification(home, monkeypatch):
    from agent import skill_preprocessing
    row = finance_skill()
    row['files'][0]['content'] += '\n${HERMES_SKILL_DIR} !`printf forbidden`\n'
    publish(home, [row], ['finance-ledger'])
    monkeypatch.setattr(skill_preprocessing, 'run_inline_shell', lambda *_a, **_kw: pytest.fail('managed inline execution'))
    from tools.skills_tool import skill_view
    value = json.loads(skill_view('finance-ledger', preprocess=True))
    assert value['success'] and '${HERMES_SKILL_DIR} !`printf forbidden`' in value['content']
