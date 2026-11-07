"""Sandbox incidents are acknowledged only after durable, profile-local native admission."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_runtime.standalone import sandbox_events as events
from tools.bot_desktop import browser_memory_guard as bridge


@pytest.fixture
def home(monkeypatch):
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home
    from hermes_constants import get_hermes_home
    path = get_hermes_home()
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hermes, '_home', None)
    activate_home(path)
    return path


def incident():
    return {'id': 'a' * 32, 'kind': 'browser_memory_guard', 'phase': 'stopped',
            'created_at': 1, 'sample': {'usage_bytes': 2200000000}, 'forced': False}


def test_identical_home_paths_in_different_pods_keep_distinct_stable_owners():
    target = {'namespace': 'agent-a', 'pod': 'sandbox-0', 'container': 'sandbox', 'uid': 'old'}
    first = bridge.owner_identity(SimpleNamespace(kubernetes_identity=target))
    assert first != bridge.owner_identity(SimpleNamespace(kubernetes_identity={**target, 'namespace': 'agent-b'}))
    assert first == bridge.owner_identity(SimpleNamespace(kubernetes_identity={**target, 'uid': 'replacement'}))


def test_retry_after_bridge_restart_deduplicates_real_native_inbox(home, monkeypatch):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        db.create_session('own-child', source='subagent', parent_session_id='agentready_home')
    finally:
        db.close()
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    monkeypatch.setattr('hermes_runtime.adapters.turns.forward_to_active_turn', lambda *_: False)
    directory = home / 'cache/browser-tabs'; directory.mkdir(parents=True)
    (directory / 'own.json').write_text(json.dumps({'tab': {'task': 'own-child'}}))
    first = events.admit(home, incident())
    second = events.admit(home, incident())
    pending = HomeInbox().pending()
    assert first['message_id'] == second['message_id']
    assert len(pending) == 1 and 'own-child' in pending[0][1]
    assert events.affected_children(home, ['own-child', 'unrelated']) == ['own-child']
    assert 'unknown' in pending[0][1] and 'payment' in pending[0][1]


def test_gateway_route_uses_real_durable_wakeup_once(home, monkeypatch):
    import sqlite3
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin',
                        lambda: {'session_key': 'own-main-route', 'profile': 'own'})
    events.admit(home, incident())
    events.admit(home, incident())
    with sqlite3.connect(home / 'state.db') as connection:
        rows = connection.execute('SELECT event_json,delivery_state FROM async_delegations').fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])['session_key'] == 'own-main-route' and rows[0][1] == 'pending'


def test_receipt_acknowledges_only_after_success_and_replays_same_incident(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, 'install', lambda *_: {'script': 'image-owned'})
    def command(_environment, _installed, action, *args):
        calls.append(action)
        return json.dumps([incident()]) if action == 'events' else ''
    monkeypatch.setattr(bridge, 'command', command)
    def failed(_event):
        raise OSError('synthetic admission unavailable')
    with pytest.raises(OSError):
        bridge.receive(object(), failed)
    assert calls == ['events']
    accepted = []
    def admitted(event):
        accepted.append(event)
        return {'accepted': True}
    assert bridge.receive(object(), admitted) == 1
    assert calls == ['events', 'events', 'ack'] and accepted == [incident()]


@pytest.mark.parametrize('receipt', [None, {}, {'accepted': False}])
def test_unreceipted_callback_keeps_incident_for_recovery(monkeypatch, receipt):
    monkeypatch.setattr(bridge, 'install', lambda *_: {'script': 'image-owned'})
    calls = []
    def command(_environment, _installed, action, *args):
        calls.append(action)
        return json.dumps([incident()])
    monkeypatch.setattr(bridge, 'command', command)
    with pytest.raises(RuntimeError, match='admission receipt'):
        bridge.receive(object(), lambda event: receipt)
    assert calls == ['events']


def test_disabled_configuration_still_drains_previously_installed_incidents(home, monkeypatch):
    (home / 'config.yaml').write_text('browser:\n  memory_guard:\n    enabled: false\n')
    bridge.marker().parent.mkdir(parents=True, exist_ok=True)
    bridge.marker().write_text('{"installed": true}')
    sandbox = object()
    monkeypatch.setattr('tools.environments.network_transport.sandbox_environment', lambda: sandbox)
    calls = []
    monkeypatch.setattr(bridge, 'receive', lambda environment, callback: calls.append(environment) or 1)
    assert events.poll(home) == 1 and calls == [sandbox]


def test_disabled_unused_guard_never_contacts_sandbox(home, monkeypatch):
    monkeypatch.setattr('tools.environments.network_transport.sandbox_environment',
                        lambda: pytest.fail('Disabled unused guard must not execute sandbox commands'))
    assert events.poll(home) == 0


def test_foreign_home_is_refused_before_sandbox_or_native_queue_access(home, tmp_path, monkeypatch):
    monkeypatch.setattr('tools.environments.network_transport.sandbox_environment',
                        lambda: pytest.fail('Foreign profile must never reach a sandbox'))
    foreign = tmp_path / 'foreign'
    with pytest.raises(RuntimeError, match='profile mismatch'):
        events.poll(foreign)
    with pytest.raises(RuntimeError, match='profile mismatch'):
        events.admit(foreign, incident())
