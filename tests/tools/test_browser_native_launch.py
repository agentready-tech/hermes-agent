"""Shared desktop Chrome uses a normal headed launch, then native CDP admission."""
import json
from types import SimpleNamespace

import pytest

from tools.bot_desktop import browser, browser_instance, runtime

pytest_plugins = ('tests.tools.browser_instance_fixture',)


def test_dock_delegates_port_allocation_to_the_browser_namespace():
    line = browser.dock_exec_line('/opt/chrome', '/own/profile', sandbox_bypass=False)
    assert '--remote-debugging-port=0' not in line
    assert 'browser_launch.py' in line
    assert '/own/profile' in line


def test_agent_first_launch_attaches_to_native_chrome(monkeypatch, tmp_path):
    from tools import browser_tool_session as session
    monkeypatch.setattr(runtime, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    monkeypatch.setattr(runtime, 'ensure_started_for_tool', lambda: None)
    monkeypatch.setattr(session, '_browser_in_sandbox', lambda: False)
    monkeypatch.setattr(browser, 'ensure_instance_endpoint', lambda *a, **kw: browser_instance.BrowserEndpoint(41327, 'a' * 64))
    monkeypatch.setattr(session._cloud, '_is_headed_mode', lambda: False)
    monkeypatch.setattr(session, '_spawn_and_collect', lambda *a, **kw:
                        {'success': True, 'data': {'cdpUrl': 'ws://localhost:41327/devtools/browser/fixture'}})
    info = {'session_name': 'own', 'features': {'local': True}}
    _, args, result = session._shared_browser_handshake('task', info, 'agent-browser', 'open', 'auto', 5)
    assert args == ['--session', info['session_name'], '--cdp', '41327']
    assert info['session_name'] != 'own' and result['native_browser_generation'] == 'a' * 64


def test_explicit_browser_request_grants_pressure_permit_before_native_launch(monkeypatch, tmp_path):
    from tools import browser_tool_session as session
    monkeypatch.setattr(runtime, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    monkeypatch.setattr(session, '_browser_in_sandbox', lambda: False)
    monkeypatch.setattr(session._cloud, '_get_browser_engine', lambda: 'auto')
    monkeypatch.setattr(session._cloud, '_is_headed_mode', lambda: False)
    monkeypatch.setattr(session, '_agent_browser_argv', lambda command: [command])
    actions = []
    monkeypatch.setattr(runtime, 'ensure_started_for_tool', lambda: actions.append('pressure-permit'))
    def launch(*a, **kw):
        assert actions == ['pressure-permit'], 'a blocked Chrome must receive its own explicit request first'
        actions.append('native-launch')
        return browser_instance.BrowserEndpoint(41327, 'a' * 64)
    monkeypatch.setattr(browser, 'ensure_instance_endpoint', launch)
    monkeypatch.setattr(session, '_spawn_and_collect', lambda *a, **kw: actions.append(a[3]) or
                        {'success': True, 'data': {'cdpUrl': 'ws://localhost:41327/devtools/browser/fixture'}})
    info = {'session_name': 'own', 'features': {'local': True}}
    result = session._dispatch_browser_command('own-task', info, 'agent-browser', 'open', ['about:blank'], 5, None)[1]
    assert result['success'] is True
    assert actions == ['pressure-permit', 'native-launch', 'get', 'open']


def test_browser_request_under_continued_pressure_never_launches_or_dispatches(monkeypatch, tmp_path):
    from tools import browser_tool_session as session
    monkeypatch.setattr(runtime, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    monkeypatch.setattr(session._cloud, '_get_browser_engine', lambda: 'auto')
    def pressure():
        raise RuntimeError('BROWSER_MEMORY_PRESSURE: wait for memory headroom')
    monkeypatch.setattr(runtime, 'ensure_started_for_tool', pressure)
    monkeypatch.setattr(browser, 'ensure_instance_endpoint', lambda *a, **kw: pytest.fail('pressure must block Chrome'))
    monkeypatch.setattr(session, '_spawn_and_collect', lambda *a, **kw: pytest.fail('pressure must block page work'))
    info = {'session_name': 'own', 'features': {'local': True}}
    result = session._dispatch_browser_command('own-task', info, 'agent-browser', 'open', ['about:blank'], 5, None)[1]
    assert result['success'] is False and result['code'] == 'browser_memory_pressure'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('permit_returncode', [0, 1])
def test_human_dock_explicit_request_uses_own_image_guard_headroom(monkeypatch, tmp_path, permit_returncode):
    import os
    from pathlib import Path
    from tools.bot_desktop import browser_launch as launch
    state = tmp_path / 'browser-watchdog'
    state.mkdir()
    image = tmp_path / 'image'
    image.mkdir()
    script = image / 'browser_watchdog.py'
    script.write_text('# image-owned watchdog\n')
    original = Path.stat
    def metadata(path, *a, **kw):
        result = original(path, *a, **kw)
        if path == script:
            values = list(result)
            values[4] = 0  # root-owned image script at the trust boundary, not a host-OS substitution.
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, 'stat', metadata)
    guard = {'state': str(state), 'executable': str(state / 'chrome'), 'script': str(script)}
    command = [guard['executable'], '--user-data-dir=' + str(tmp_path / 'profile')]
    actions = []
    def permit(args, **options):
        assert args == ['python3', str(script), 'permit', '--state', str(state)]
        actions.append('headroom-permit')
        return SimpleNamespace(returncode=permit_returncode)
    monkeypatch.setattr(launch.subprocess, 'run', permit)
    monkeypatch.setattr(launch, 'ensure', lambda args: actions.append('launch') or {'state': 'attachable', 'port': 41327})
    if permit_returncode:
        with pytest.raises(RuntimeError, match='memory headroom'):
            launch.dock(['--guard', json.dumps(guard), *command])
        assert actions == ['headroom-permit']
    else:
        assert launch.dock(['--guard', json.dumps(guard), *command])['port'] == 41327
        assert actions == ['headroom-permit', 'launch']


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('state', ['attachable', 'owned', 'blocked', 'unknown'])
def test_existing_owner_is_never_relaunched(monkeypatch, tmp_path, state):
    from tools.bot_desktop import browser_launch as launch
    profile = tmp_path / 'profile'
    result = {'state': state, **({'port': 41327} if state == 'attachable' else {})}
    monkeypatch.setattr(launch.instance, 'inspect_profile', lambda *a, **kw: result)
    monkeypatch.setattr(launch.subprocess, 'Popen', lambda *a, **kw: pytest.fail('occupied profile launched'))
    argv = browser.dock_argv('/opt/chrome', str(profile), sandbox_bypass=False)
    if state in ('blocked', 'unknown'):
        with pytest.raises(launch.instance.ProfileUnavailable):
            launch.ensure(argv, exclude_session='own')
    else:
        assert launch.ensure(argv, exclude_session='own') == result
    assert not (profile / 'DevToolsActivePort').exists()


@pytest.mark.platforms('posix')
def test_first_launch_uses_a_positive_loopback_port_and_common_native_flags(monkeypatch, tmp_path):
    from tools.bot_desktop import browser_launch as launch
    profile = tmp_path / 'profile'
    monkeypatch.setattr(launch.instance, 'inspect_profile', lambda *a, **kw: {'state': 'absent'})
    captured = []
    monkeypatch.setattr(launch.subprocess, 'Popen', lambda argv, **kw: captured.append((argv, kw)) or object())
    monkeypatch.setattr(launch, '_wait_ready', lambda p, proc, port, timeout: {'state': 'attachable', 'port': port})
    result = launch.ensure(browser.dock_argv('/own/guarded-chrome', str(profile), sandbox_bypass=False))
    command = captured[0][0]
    assert command[0] == '/own/guarded-chrome'
    assert command.count('--remote-debugging-port=' + str(result['port'])) == 1
    assert 0 < result['port'] < 65536
    assert '--remote-debugging-address=127.0.0.1' in command
    assert '--remote-debugging-pipe' not in command and '--enable-automation' not in command
    assert '--headless' not in command
    assert browser.dock_argv('/own/guarded-chrome', str(profile), sandbox_bypass=False) == command[:-2]
    assert not (profile / 'DevToolsActivePort').exists(), 'only verified readiness may publish discovery'


@pytest.mark.platforms('posix')
def test_positive_endpoint_published_only_for_native_profile_process(monkeypatch, tmp_path, native_chrome):
    from tools.bot_desktop import browser_launch as launch
    profile = tmp_path / 'profile'
    with native_chrome(profile) as pid:
        target = profile / 'DevToolsActivePort'
        port = int(target.read_text().splitlines()[0])
        target.unlink()
        monkeypatch.setattr(launch, '_endpoint', lambda value: '/devtools/browser/native-fixture')
        endpoint = browser_instance.admitted_endpoint(launch._publish(profile, pid, port))
        assert endpoint.port == port
        assert endpoint == browser_instance.admitted_endpoint(browser_instance.inspect_profile(profile))
        assert target.read_text() == f'{port}\n/devtools/browser/native-fixture\n'
        assert target.stat().st_mode & 0o777 == 0o600
        before = target.read_bytes()
        import socket
        with socket.socket() as foreign:
            foreign.bind(('127.0.0.1', 0)); foreign.listen()
            with pytest.raises(ValueError, match='does not own'):
                launch._publish(profile, pid, foreign.getsockname()[1])
        assert target.read_bytes() == before


@pytest.mark.platforms('posix')
def test_changed_generation_cannot_publish_discovery(monkeypatch, tmp_path, native_chrome):
    from tools.bot_desktop import browser_launch as launch
    profile = tmp_path / 'profile'
    with native_chrome(profile) as pid:
        target = profile / 'DevToolsActivePort'
        port = int(target.read_text().splitlines()[0])
        target.unlink()
        original = launch.instance._process
        def endpoint(value):
            def changed(value):
                result = original(value)
                return {**result, 'start': 'recycled'} if value == pid else result
            monkeypatch.setattr(launch.instance, '_process', changed)
            return '/devtools/browser/native-fixture'
        monkeypatch.setattr(launch, '_endpoint', endpoint)
        with pytest.raises(ValueError, match='ownership changed'):
            launch._publish(profile, pid, port)
        assert not target.exists()


@pytest.mark.platforms('posix')
def test_same_number_on_owner_ipv6_and_foreign_ipv4_is_not_owned_cdp(tmp_path, native_chrome):
    import socket
    from tools.bot_desktop import browser_launch as launch
    profile = tmp_path / 'profile'
    with native_chrome(profile, ipv6=True) as pid:
        target = profile / 'DevToolsActivePort'
        original = target.read_bytes()
        port = int(original.splitlines()[0])
        owner = launch.instance._process(pid)
        with socket.socket() as foreign:
            foreign.bind(('127.0.0.1', port)); foreign.listen()
            assert launch.instance._owns_listener(owner, port)
            assert not launch.instance._owns_listener(owner, port, address='127.0.0.1')
            with pytest.raises(ValueError, match='does not own'):
                launch._publish(profile, pid, port)
        assert target.read_bytes() == original


@pytest.mark.parametrize('url', ['ws://other:41327/devtools/browser/a',
                                 'ws://127.0.0.1:41328/devtools/browser/a',
                                 'ws://127.0.0.1:41327/devtools/page/a',
                                 'ws://127.0.0.1:41327/devtools/browser/a?foreign=yes'])
def test_discovery_rejects_a_foreign_endpoint(monkeypatch, url):
    from tools.bot_desktop import browser_launch as launch
    reply = SimpleNamespace(status=200, read=lambda limit: json.dumps({'webSocketDebuggerUrl': url}).encode())
    connection = SimpleNamespace(request=lambda *a: None, getresponse=lambda: reply, close=lambda: None)
    monkeypatch.setattr(launch.http.client, 'HTTPConnection', lambda *a, **kw: connection)
    with pytest.raises(ValueError, match='identity is invalid'):
        launch._endpoint(41327)


def test_dock_refresh_changes_only_own_browser_exec(tmp_path):
    from tools.bot_desktop import browser_launch as launch
    directory = tmp_path / 'xfce4/panel/launcher-22'
    directory.mkdir(parents=True)
    own = directory / 'hermes.desktop'
    own.write_text('[Desktop Entry]\nName=Browser\nExec=old\nIcon=internet-web-browser\n')
    unrelated = directory / 'user.desktop'
    unrelated.write_text('Name=Browser\nExec=personal\n')
    launch.refresh_dock(tmp_path, 'Exec="python3" "native launch.py" "--dock"')
    assert own.read_text() == ('[Desktop Entry]\nName=Browser\n'
                               'Exec="python3" "native launch.py" "--dock"\nIcon=internet-web-browser\n')
    assert unrelated.read_text() == 'Name=Browser\nExec=personal\n'


def test_existing_display_refreshes_dock_at_tool_boundary_without_spawning(monkeypatch):
    from tools.bot_desktop import browser_memory_guard
    monkeypatch.setattr(browser_memory_guard, 'configuration', lambda: {'enabled': False})
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    actions = []
    monkeypatch.setattr(runtime, 'start', lambda: actions.append('adopt-and-refresh'))
    monkeypatch.setattr(runtime, 'touch_activity', lambda: actions.append('touch'))
    runtime.ensure_started_for_tool()
    assert actions == ['adopt-and-refresh', 'touch']


def test_live_sandbox_refresh_does_not_restart_desktop_or_browser(monkeypatch):
    from tools.bot_desktop import sandbox_host, browser_memory
    from tools.environments import streams
    environment = SimpleNamespace()
    monkeypatch.setattr(browser_memory, 'configure', lambda env: None)
    monkeypatch.setattr(sandbox_host, '_remote_dir', lambda *a: '/own/scratch')
    monkeypatch.setattr(sandbox_host, '_published', lambda *a: {'DISPLAY': ':20'})
    monkeypatch.setattr(sandbox_host, 'desktop_config_dir', lambda *a: '/own/config')
    monkeypatch.setattr(sandbox_host, 'browser_launch_path', lambda e: '/own/native/browser_launch.py')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda e: 'pn')
    monkeypatch.setattr(sandbox_host, '_record', lambda e, d, p, live: live)
    monkeypatch.setattr(sandbox_host, 'stop', lambda *a: pytest.fail('live desktop must not stop'))
    calls = []
    monkeypatch.setattr(streams, 'run_in', lambda e, cmd, **kw: calls.append((cmd, kw)) or
                        SimpleNamespace(returncode=0))
    assert sandbox_host.start(environment, 'own', geometry='1024x768',
                              browser_exec_line='Exec="python3" "own-launcher"') == {'DISPLAY': ':20'}
    assert calls[0][0] == ['python3', '/own/native/browser_launch.py', '--refresh-dock']
    assert json.loads(calls[0][1]['stdin']) == {'config_home': '/own/config',
                                             'exec_line': 'Exec="python3" "own-launcher"'}
    assert calls[0][1]['user'] == 'pn'


def test_sandbox_allocation_runs_as_own_user_inside_remote_namespace(monkeypatch):
    from tools.bot_desktop import sandbox_host, browser_memory_guard
    from tools.environments import streams
    env = SimpleNamespace()
    monkeypatch.setattr(sandbox_host, 'chromium_executable', lambda e: '/own/chrome')
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda e: '/own/profile')
    monkeypatch.setattr(sandbox_host, 'browser_launch_path', lambda e: '/own/launcher/browser_launch.py')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda e: 'pn')
    monkeypatch.setattr(browser_memory_guard, 'executable', lambda e, exe: '/own/guarded-chrome')
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20', 'XAUTHORITY': '/own/Xauthority'})
    calls = []
    monkeypatch.setattr(streams, 'run_in', lambda e, argv, **kw: calls.append((e, argv, kw)) or
                        SimpleNamespace(returncode=0, stdout=json.dumps({'state': 'attachable', 'port': 41327,
                                                                        'generation': 'a' * 64}).encode()))
    assert sandbox_host.ensure_browser_endpoint(env, exclude_session='own-child') == browser_instance.BrowserEndpoint(41327, 'a' * 64)
    target, command, options = calls[0]
    assert target is env and command == ['python3', '/own/launcher/browser_launch.py']
    assert options['user'] == 'pn'
    assert options['child_env']['DISPLAY'] == ':20'
    request = json.loads(options['stdin'])
    assert request['exclude_session'] == 'own-child'
    assert request['argv'][0] == '/own/guarded-chrome'
    assert '--user-data-dir=/own/profile' in request['argv']
    assert not any(arg.startswith('--remote-debugging-port=') for arg in request['argv'])


def test_concurrent_native_helper_seed_can_always_be_imported(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    import sys
    from pathlib import Path
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    monkeypatch.setattr(sandbox_host, 'desktop_home', lambda env: str(tmp_path))
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env: 'own-desktop-user')
    def run(env, argv, **options):
        assert options['user'] == 'own-desktop-user'
        return subprocess.run([sys.executable, *argv[1:]], input=options['stdin'],
                              capture_output=True, timeout=15, check=False)
    monkeypatch.setattr(streams, 'run_in', run)
    def seed_and_import(_):
        target = Path(sandbox_host.browser_launch_path(SimpleNamespace()))
        command = ('import runpy,sys;sys.path.insert(0,sys.argv[1]);'
                   'value=runpy.run_path(sys.argv[2]);assert callable(value["ensure"])')
        subprocess.run([sys.executable, '-c', command, str(target.parent), str(target)],
                       capture_output=True, timeout=15, check=True)
        return target
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(seed_and_import, range(8)))
    assert len(set(paths)) == 1
    assert paths[0].stat().st_mode & 0o777 == 0o600
    initial = paths[0].stat().st_mtime_ns
    assert seed_and_import(None) == paths[0]
    assert paths[0].stat().st_mtime_ns == initial, 'an identical seed never truncates a readable helper'
