"""The optional standalone UI never owns headless gateway readiness."""
import os
from types import SimpleNamespace

import pytest

from hermes_runtime.standalone import supervisor


@pytest.mark.parametrize("with_dashboard", [None, False, True])
def test_run_selects_optional_dashboard_without_changing_workers(tmp_path, monkeypatch, with_dashboard):
    captured = {}
    config = {'enabled': True, 'host': '127.0.0.1', 'port': 9119,
              'workspace': {'native': True},
              'panel': {'url': 'https://panel.invalid'}}
    if with_dashboard is not None:
        config['dashboard'] = {'enabled': with_dashboard}
    monkeypatch.setattr(supervisor, 'initialize', lambda home: {'autonomous': config})
    monkeypatch.setattr(supervisor, 'child_environment', lambda home: {})
    monkeypatch.setattr(supervisor.Supervisor, 'run', lambda self, prepare: captured.update(specs=self.specs))
    monkeypatch.setattr('hermes_runtime.standalone.service._command', lambda module, **kwargs: [module])
    monkeypatch.setattr('hermes_runtime.standalone.service._argv', lambda home, python, action: [action])
    supervisor.run(tmp_path)
    expected = {'gateway', 'workspace', 'computer'}
    if with_dashboard is not False:
        expected.add('backend')
    assert set(captured['specs']) == expected


def test_headless_status_uses_gateway_liveness_without_dashboard_http(tmp_path, monkeypatch):
    owner = supervisor.Supervisor(tmp_path, {'gateway': []}, port=9119)
    owner.children['gateway'] = SimpleNamespace(pid=os.getpid())
    owner.publish()
    monkeypatch.setattr(supervisor, 'urlopen', lambda *a, **k: pytest.fail('disabled dashboard was probed'))
    monkeypatch.setattr('gateway.status.read_runtime_status', lambda *a: {'pid': os.getpid(), 'gateway_state': 'running'})
    monkeypatch.setattr('gateway.status.runtime_status_is_stale', lambda value: False)
    monkeypatch.setattr('gateway.status.runtime_status_pid_is_live', lambda value: True)
    assert supervisor.status(tmp_path)['ok']
    monkeypatch.setattr('gateway.status.runtime_status_is_stale', lambda value: True)
    assert not supervisor.status(tmp_path)['ok']


def test_headless_init_preserves_keys_without_creating_dashboard_credentials(tmp_path):
    from hermes_runtime.standalone.config import initialize
    tmp_path.joinpath('config.yaml').write_text('autonomous:\n  dashboard:\n    enabled: false\n')
    tmp_path.joinpath('.env').write_text('EXISTING_KEY=fixture\n')
    initialize(tmp_path)
    from dotenv import dotenv_values
    assert set(dotenv_values(tmp_path / '.env')) == {'EXISTING_KEY'}
    assert dotenv_values(tmp_path / '.env')['EXISTING_KEY'] == 'fixture'


def test_dashboard_default_and_invalid_flags():
    from hermes_runtime.standalone.config import dashboard_enabled
    assert dashboard_enabled({})
    assert dashboard_enabled({'dashboard': {'enabled': True}})
    for value in ('false', 0, None):
        with pytest.raises(ValueError, match='must be true or false'):
            dashboard_enabled({'dashboard': {'enabled': value}})
