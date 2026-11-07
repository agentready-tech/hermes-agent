"""A locked shared profile is not permission to launch a second Chromium."""
import os
import socket

import pytest

from tools import browser_tool_session as session
from tools.bot_desktop import browser, browser_instance, runtime
pytest_plugins = ('tests.tools.browser_instance_fixture',)

pytestmark = pytest.mark.platforms('posix')


def _dispatch_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'state_dir', lambda: tmp_path / 'desktop')
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    monkeypatch.setattr(session, '_browser_in_sandbox', lambda: False)
    monkeypatch.setattr(session._cloud, '_get_browser_engine', lambda: 'auto')
    monkeypatch.setattr(session._cloud, '_is_headed_mode', lambda: False)
    monkeypatch.setattr(session, '_agent_browser_argv', lambda command: [command])
    return {'session_name': 'sibling', 'features': {'local': True}}


def test_unverifiable_live_profile_owner_does_not_launch_another_browser(monkeypatch, tmp_path):
    profile = tmp_path / 'browser-profile'
    profile.mkdir()
    (profile / 'SingletonLock').symlink_to(f'{socket.gethostname()}-{os.getpid()}')
    monkeypatch.setattr(browser, 'profile_dir', lambda: profile)
    info = _dispatch_setup(monkeypatch, tmp_path)
    launches = []
    monkeypatch.setattr(session, '_spawn_and_collect', lambda *args: launches.append(args) or {'success': True})

    _, result = session._dispatch_browser_command('task', info, 'agent-browser', 'get', ['cdp-url'], 5, None)

    assert result.get('success') is False
    assert result.get('code') == 'browser_profile_unavailable'
    assert launches == []
    assert (profile / 'SingletonLock').is_symlink(), 'unknown ownership never authorizes removing locks'


def test_native_live_owner_without_port_blocks_a_second_session_but_own_pipe_still_works(tmp_path, native_chrome):
    profile = tmp_path / 'profile'
    with native_chrome(profile, pipe=True, session='own'):
        lock = (profile / 'SingletonLock').lstat()
        assert browser_instance.inspect_profile(profile, exclude_session='sibling') == {'state': 'blocked'}
        assert browser_instance.inspect_profile(profile, exclude_session='own') == {'state': 'owned'}
        assert browser.running_instance_cdp_port(str(profile), exclude_session='own') is None
        with pytest.raises(browser_instance.ProfileUnavailable):
            browser.running_instance_cdp_port(str(profile), exclude_session='sibling')
        assert (profile / 'SingletonLock').lstat() == lock


def test_native_dead_generation_allows_native_recovery_without_touching_profile_data(tmp_path, native_chrome):
    profile = tmp_path / 'profile'
    with native_chrome(profile) as first:
        (profile / 'saved-login').write_text('fixture-profile-state')
    lock = (profile / 'SingletonLock').lstat()
    assert browser_instance.inspect_profile(profile) == {'state': 'stale'}
    assert browser.running_instance_cdp_port(str(profile)) is None
    assert (profile / 'SingletonLock').lstat() == lock, 'Hermes does not unlink singleton files'
    with native_chrome(profile) as second:
        assert first != second
        assert browser_instance.inspect_profile(profile)['state'] == 'attachable'
        assert (profile / 'saved-login').read_text() == 'fixture-profile-state'


def test_a_live_owner_is_not_absent_when_its_lock_was_removed(tmp_path, native_chrome):
    profile = tmp_path / 'profile'
    with native_chrome(profile):
        (profile / 'SingletonLock').unlink()
        assert browser_instance.inspect_profile(profile) == {'state': 'unknown'}
        with pytest.raises(browser_instance.ProfileUnavailable):
            browser.running_instance_cdp_port(str(profile))


def test_a_recycled_devtools_port_is_never_borrowed_from_another_process(tmp_path, native_chrome):
    profile = tmp_path / 'profile'
    with native_chrome(profile), socket.socket() as unrelated:
        unrelated.bind(('127.0.0.1', 0)); unrelated.listen()
        (profile / 'DevToolsActivePort').write_text(str(unrelated.getsockname()[1]) + '\n')
        assert browser_instance.inspect_profile(profile) == {'state': 'blocked'}


def test_pid_generation_change_during_admission_is_unknown(monkeypatch, tmp_path, native_chrome):
    profile = tmp_path / 'profile'
    with native_chrome(profile, pipe=True) as pid:
        original = browser_instance._process
        calls = []
        def changing(value):
            result = original(value)
            if value == pid:
                calls.append(1)
                if len(calls) > 1:
                    result['start'] = 'recycled'
            return result
        monkeypatch.setattr(browser_instance, '_process', changing)
        assert browser_instance.inspect_profile(profile) == {'state': 'unknown'}


def test_launch_fence_ends_before_page_work_and_does_not_lock_other_profiles(monkeypatch, tmp_path):
    import fcntl
    info = _dispatch_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(browser, 'running_instance_cdp_port', lambda *args, **kw: None)
    events = []
    other = tmp_path / 'other-profile'
    other.mkdir()
    def spawn(task, state, parts, command, *args):
        path = runtime.state_dir() / 'browser-launch.lock'
        with path.open('a+') as lock, (other / 'browser-launch.lock').open('a+') as foreign:
            fcntl.flock(foreign, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if command == 'get':
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            events.append(command)
        return {'success': True, 'data': {'cdpUrl': 'fixture'}}
    monkeypatch.setattr(session, '_spawn_and_collect', spawn)
    _, result = session._dispatch_browser_command('task', info, 'agent-browser', 'open', ['about:blank'], 5, None)
    assert result['success']
    assert events == ['get', 'open']


def test_close_never_starts_or_adopts_a_siblings_browser(monkeypatch, tmp_path):
    info = _dispatch_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(browser, 'running_instance_cdp_port', lambda *args, **kw: pytest.fail('close must not adopt'))
    commands = []
    monkeypatch.setattr(session, '_spawn_and_collect', lambda *args: commands.append(args[3]) or {'success': True})
    assert session._dispatch_browser_command('task', info, 'agent-browser', 'close', [], 5, None)[1]['success']
    assert commands == ['close']


def test_chrome_single_string_argv_does_not_turn_a_renderer_into_a_browser_root(tmp_path):
    profile = str(tmp_path / 'profile')
    child = browser_instance._argv(('chrome --type=renderer --user-data-dir=' + profile).encode())
    assert browser_instance._flag(child, '--type') == 'renderer'
    assert browser_instance._flag(child, '--user-data-dir') == profile


@pytest.mark.parametrize('returncode,output', [(1, b''), (0, b''), (0, b'not-json'), (0, b'[]'),
                                             (0, b'{"state":[]}'), (0, b'{"state":"attachable","port":true}')])
def test_failed_sandbox_probe_never_becomes_permission_to_launch(monkeypatch, returncode, output):
    from types import SimpleNamespace
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda _: '/fixture/profile')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda _: 'pn')
    monkeypatch.setattr(streams, 'run_in', lambda *a, **kw: SimpleNamespace(returncode=returncode, stdout=output))
    with pytest.raises(browser_instance.ProfileUnavailable):
        sandbox_host.browser_instance_cdp_port(object())
