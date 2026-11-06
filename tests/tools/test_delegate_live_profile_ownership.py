"""A rebuilt main agent owns its DB's children, never a same-id parent in another DB."""
from contextlib import contextmanager
import json
import weakref

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tools.delegate_tool import delegate_task
from tools import delegate_tool_registry as registry


@contextmanager
def scoped(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


class Actor:
    def __init__(self, db, session_id='agentready_home'):
        self._session_db, self.session_id = db, session_id
        self.steered = []

    def steer(self, text):
        self.steered.append(text)
        return True


def test_same_parent_id_cannot_list_steer_or_stop_another_profiles_live_child(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, '_active_subagents', {})
    homes, parents, children, databases = {}, {}, {}, []
    interrupted = []
    monkeypatch.setattr(registry, 'request_hard_interrupt', lambda actor, reason: interrupted.append(actor) or True)
    try:
        for name in ('alpha', 'bravo'):
            home = homes[name] = tmp_path / name
            home.mkdir()
            db = SessionDB(db_path=home / 'state.db')
            databases.append(db)
            original = Actor(db)
            child = children[name] = Actor(db, 'child-' + name)
            child._delegate_parent_ref = weakref.ref(original)
            parents[name] = Actor(db)  # A rebuilt parent: same durable id, new object.
            with scoped(home):
                registry._register_subagent(dict(subagent_id='live-' + name, agent=child,
                    owner_agent_session_id='agentready_home', goal=name + '-private-goal'))
        for name, home in homes.items():
            other = 'bravo' if name == 'alpha' else 'alpha'
            with scoped(home):
                payload = json.loads(delegate_task(action='list', parent_agent=parents[name]))
                assert [r['subagent_id'] for r in payload['subagents']] == ['live-' + name]
                assert other + '-private-goal' not in json.dumps(payload)
                for action in ('steer', 'stop'):
                    result = json.loads(delegate_task(action=action, subagent_id='live-' + other,
                        message='synthetic steer', parent_agent=parents[name]))
                    assert 'error' in result
                assert [r['subagent_id'] for r in registry.list_active_subagents()] == ['live-' + name]
                assert '_profile_home' not in json.dumps(registry.list_active_subagents())
        assert not interrupted
        assert not any(c.steered for c in children.values())
    finally:
        for db in databases:
            db.close()
