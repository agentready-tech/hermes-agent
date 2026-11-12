"""A replaced shared Chrome must not reuse a tool client's previous CDP binding."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from tools import browser_tab_runtime, browser_tool_session as session, browser_use_cli as browser_use
from tools.bot_desktop import browser_instance, browser_memory_guard, runtime, sandbox_host
from tools.environments import streams


def run_program(code, task='parent'):
    return json.loads(browser_use.browser_exec(code, task_id=task))


@pytest.fixture
def native_route(monkeypatch, tmp_path):
    env = object()
    route = SimpleNamespace(endpoint=browser_instance.BrowserEndpoint(36655, 'a' * 64),
                            daemons={}, handshakes=[], actions=[], executions=[], admissions=[],
                            result='success', change_during_probe=None)
    sessions = {}

    def current_session(task):
        return sessions.setdefault(task, {'session_name': 'client-' + task, 'features': {'local': True}})

    monkeypatch.setattr(session, '_browser_command_preflight', lambda: {'browser_cmd': 'agent-browser'})
    monkeypatch.setattr(session, '_get_session_info', current_session)
    monkeypatch.setattr(session, '_agent_browser_argv', lambda command: [command])
    monkeypatch.setattr(session, '_browser_in_sandbox', lambda: True)
    monkeypatch.setattr(session, '_recycle_local_session', lambda *a, **k: pytest.fail('no unsafe recycle'))
    monkeypatch.setattr(session._cloud, '_get_browser_engine', lambda: 'auto')
    monkeypatch.setattr(session._cloud, '_get_cloud_provider', lambda: None)
    monkeypatch.setattr(session._cloud, '_is_headed_mode', lambda: True)
    monkeypatch.setattr(session._cdp, '_get_cdp_override', lambda: '')
    monkeypatch.setattr(session._cdp, '_ensure_cdp_supervisor', lambda _: None)
    monkeypatch.setattr(runtime, 'state_dir', lambda: tmp_path / 'desktop')
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    monkeypatch.setattr(runtime, 'ensure_started_for_tool', lambda: None)
    monkeypatch.setattr(runtime, '_sandbox_env', lambda **_: env)
    monkeypatch.setattr(sandbox_host, '_user_for', lambda _: 'pn')
    monkeypatch.setattr(sandbox_host, 'chromium_executable', lambda _: '/own/chrome')
    monkeypatch.setattr(sandbox_host, 'browser_launch_path', lambda _: '/own/browser_launch.py')
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda _: '/own/profile')
    monkeypatch.setattr(sandbox_host, 'chromium_sandbox_bypass', lambda _: False)
    monkeypatch.setattr(browser_memory_guard, 'executable', lambda _, exe: exe)
    monkeypatch.setattr(browser_memory_guard, 'tool_preflight_error', lambda: None)

    def sandbox_probe(target, args, *, user, **_):
        assert target is env and user == 'pn'
        route.admissions.append('inspect' if args[1] == '-c' else 'ensure')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'state': 'attachable',
                               'port': route.endpoint.port, 'generation': route.endpoint.generation}).encode())

    def spawn(task, info, args, command, *_):
        name = args[args.index('--session') + 1]
        assert name == info['session_name']
        port = args[args.index('--cdp') + 1]
        if command != 'get':
            route.actions.append((task, command, name, port))
            assert command != 'close'
            if route.result == 'action-refused':
                return {'success': False, 'returncode': 101, 'error': 'Connection refused'}
            return {'success': True}
        route.handshakes.append((task, name, port))
        if route.result == 'probe-refused':
            return {'success': False, 'error': 'CDP connection refused'}
        boot = route.daemons.setdefault(name, route.endpoint)
        if route.change_during_probe is not None:
            route.endpoint, route.change_during_probe = route.change_during_probe, None
        if boot != route.endpoint:
            return {'success': False, 'error': 'CDP connection refused'}
        return {'success': True, 'data': {'cdpUrl': f'ws://127.0.0.1:{port}/devtools/browser/fixture'}}

    def harness(cmd, code, env, timeout):
        route.executions.append((code, env['BU_NAME'], env['BU_CDP_WS']))
        if route.result == 'timeout':
            raise subprocess.TimeoutExpired(cmd, timeout)
        rc = int(route.result == 'refused')
        return subprocess.CompletedProcess(cmd, rc, 'Connection refused' if rc else 'ok', '')

    monkeypatch.setattr(streams, 'run_in', sandbox_probe)
    monkeypatch.setattr(session, '_spawn_and_collect', spawn)
    monkeypatch.setattr(browser_use, '_find_cli', lambda: ['browser-harness'])
    monkeypatch.setattr(browser_use, '_base_subprocess_env', dict)
    monkeypatch.setattr(browser_use, '_real_profile_consented', lambda: False)
    monkeypatch.setattr(browser_use, '_reach_sandbox_cdp', lambda cdp: cdp)
    monkeypatch.setattr(browser_use, '_attach_vault_supervisor', lambda *_: None)
    monkeypatch.setattr(browser_use, '_run_cli_killing_process_group', harness)
    monkeypatch.setattr(browser_use, '_driven_daemons', set())
    monkeypatch.setattr(browser_tab_runtime, '_contexts', {})
    monkeypatch.setattr(browser_tab_runtime, '_completed_tasks', set())
    route.session = current_session
    return route


@pytest.mark.parametrize('new_port', [36655, 56353], ids=['port-reused', 'new-port'])
def test_new_chrome_rebinds_both_clients_once_and_only_for_the_calling_task(native_route, new_port):
    route = native_route
    assert run_program('before = 1')['success']
    assert run_program('unrelated = 1', 'child')['success']
    first_name = route.handshakes[0][1]
    sibling = route.session('child')['session_name']
    first_harness = route.executions[0][1]
    route.endpoint = browser_instance.BrowserEndpoint(new_port, 'b' * 64)
    assert run_program('after = 1')['success']
    assert run_program('again = 1')['success']
    second, third = route.handshakes[2:]
    assert second == third and second[1] != first_name and second[2] == str(new_port)
    assert route.session('child')['session_name'] == sibling
    assert route.executions[2][1] == route.executions[3][1] != first_harness
    assert len(route.executions) == 4 and 'after = 1' in route.executions[2][0]
    assert route.admissions == ['ensure'] * 4
    assert route.actions == []


def test_handshake_race_rechecks_owner_once_before_running_any_program(native_route):
    route = native_route
    route.change_during_probe = browser_instance.BrowserEndpoint(56353, 'b' * 64)
    assert run_program('only_once = 1')['success']
    assert route.admissions == ['ensure', 'inspect']
    assert [row[2] for row in route.handshakes] == ['36655', '56353']
    assert route.handshakes[0][1] != route.handshakes[1][1]
    assert len(route.executions) == 1 and 'only_once = 1' in route.executions[0][0]


def test_failed_probe_does_not_dispatch_when_chrome_generation_is_unchanged(native_route):
    route = native_route
    route.result = 'probe-refused'
    assert 'CDP connection refused' in run_program('never = 1')['error']
    assert route.admissions == ['ensure', 'inspect']
    assert len(route.handshakes) == 1 and route.executions == []


@pytest.mark.parametrize('result', ['timeout', 'refused'])
def test_failure_after_browser_exec_dispatch_never_replays_user_program(native_route, result):
    route = native_route
    route.result = result
    outcome = run_program('only_once = 1')
    assert not outcome.get('success')
    message = outcome.get('error') if result == 'timeout' else outcome.get('output')
    assert ('timed out' if result == 'timeout' else 'Connection refused') in message
    assert len(route.executions) == len(route.handshakes) == 1
    assert route.admissions == ['ensure']


def test_shared_page_action_never_retries_after_the_client_returns_a_protocol_failure(native_route):
    route = native_route
    route.result = 'action-refused'
    assert not session._run_browser_command('parent', 'click', ['fixture'])['success']
    assert [row[1] for row in route.actions] == ['click']
    assert len(route.handshakes) == 1


def test_explicit_remote_cdp_session_has_no_native_generation_binding(native_route):
    route = native_route
    info = route.session('remote')
    info.update(cdp_url='ws://remote.example:2222/devtools/browser/fixture', features={'cdp_override': True})
    assert session._run_browser_command('remote', 'open', ['about:blank'])['success']
    assert route.admissions == [] and route.handshakes == []
    assert route.actions == [('remote', 'open', 'client-remote', info['cdp_url'])]
