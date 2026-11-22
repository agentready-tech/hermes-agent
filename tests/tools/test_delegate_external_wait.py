"""AGE-6 project wait identity and ownership on the native delegate dispatch."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.fixture
def owner(main_home):
    from hermes_runtime.sessions import HOME_SESSION_ID

    _, db = main_home
    return SimpleNamespace(_session_db=db, session_id=HOME_SESSION_ID, _agentready_active_turn=SimpleNamespace())


def _call(owner, **args):
    from run_agent import AIAgent

    return json.loads(AIAgent._dispatch_delegate_task(owner, {'action': 'external_wait', **args}))


def _args(project):
    return {'op': 'open', 'project_id': project, 'mode': 'resume_main',
            'reply_criterion': 'Supplier confirms receipt', 'return_path': 'timer',
            'deadline': (datetime.now(UTC) + timedelta(hours=2)).isoformat()}


def test_native_wait_requires_an_owned_project_and_live_main(owner, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.delegate_project_store import ProjectStore

    db = owner._session_db
    db.create_session('separate', source='cli')
    foreign = SimpleNamespace(_session_db=db, session_id='separate')
    with ProjectStore(foreign) as store:
        other = store.create('other-order', 'Different order', '')['subagent_id']
    assert _call(owner, **_args(other)).get('error')
    with ProjectStore(owner) as store:
        project = store.create('order', 'Check supplier reply', '')['subagent_id']
    with monkeypatch.context() as m:
        m.setattr(owner, 'side_agent', True, raising=False)
        assert _call(owner, **_args(project)).get('error')
    token = set_hermes_home_override(str(db.db_path) + '-another-profile')
    try:
        assert _call(owner, **_args(project)).get('error')
    finally:
        reset_hermes_home_override(token)
    assert _call(owner, **{**_args(project), 'return_path': 'event'}).get('error')
    with ProjectStore(owner) as store:
        assert 'external_wait' not in store.read_workspace(project)


def test_wait_open_select_and_cancel_preserve_project_workspace(owner):
    from tools.delegate_project_store import ProjectStore

    owner._session_db.append_message(owner.session_id, 'user', 'An old reply')
    with ProjectStore(owner) as store:
        project = store.create('order', 'Check supplier reply', '')['subagent_id']
        store.save_workspace(project, {'branch': 'existing-worktree'})
    args = _args(project)
    opened = _call(owner, **args)
    assert opened['success'] and opened['state'] == 'OPEN' and opened['job_id'] is None
    assert opened['input_watermarks']['messages'] > 0
    assert _call(owner, **args) == opened
    assert _call(owner, **{**args, 'reply_criterion': 'Different criterion'}).get('error')
    assert owner._agentready_active_turn.external_wait == (project, opened['wait_id'])
    owner._agentready_active_turn = SimpleNamespace()
    assert not hasattr(owner._agentready_active_turn, 'external_wait')
    ids = {'project_id': project, 'wait_id': opened['wait_id']}
    assert _call(owner, op='select', **ids) == opened
    assert _call(owner, op='close', reason='reply', **ids).get('error')
    with ProjectStore(owner) as store:
        store.save_workspace(project, {'branch': 'updated-worktree'})
        workspace = store.read_workspace(project)
        assert workspace['worktree'] == {'branch': 'updated-worktree'}
        assert workspace['external_wait']['wait_id'] == opened['wait_id']
    assert _call(owner, op='close', reason='cancelled', **ids)['state'] == 'CLOSED'
    assert owner._agentready_active_turn.external_wait is None
    assert _call(owner, op='select', **ids).get('error')
    assert _call(owner, **args)['wait_id'] != opened['wait_id']


def test_open_marks_native_input_high_water_after_rows_are_deleted(owner):
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch
    from tools.delegate_project_store import ProjectStore

    db = owner._session_db
    epoch = begin_runtime_epoch(db, instance_id='isolated-test')
    admission = admit_session_input(db, epoch=epoch, principal_id='fixture', session_id=owner.session_id,
                                    request_id='previous-input', payload={'text': 'Old input'})
    db._execute_write(lambda conn: conn.execute('DELETE FROM session_admissions'))
    owner._session_db.append_message(owner.session_id, 'user', 'Old transcript input')
    with db._read_ctx() as conn:
        high_water = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()[0]
    db._execute_write(lambda conn: conn.execute('DELETE FROM messages'))

    with ProjectStore(owner) as store:
        project = store.create('order', 'Check supplier reply', '')['subagent_id']
    args = _args(project)
    opened = _call(owner, **args)
    assert opened['input_watermarks'] == {'messages': high_water, 'session_admissions': admission['seq']}
    db.append_message(owner.session_id, 'user', 'New input')
    assert _call(owner, **args) == opened
