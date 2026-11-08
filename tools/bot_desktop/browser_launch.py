"""Launch the shared headed browser in its own namespace, then verify native CDP.

Chromium's port=0 enables its automation signal. A positive loopback port avoids
that signal, but Chromium does not publish DevToolsActivePort in that mode. Only
the verified profile/PID/listener generation may publish that native discovery file.
This helper is also seeded beside browser_instance.py inside the desktop sandbox.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

if __package__:
    from . import browser_instance as instance
else:
    import browser_instance as instance


def _positive_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def _endpoint(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
    try:
        connection.request('GET', '/json/version')
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Native browser CDP did not answer')
        value = json.loads(response.read(65536))
    finally:
        connection.close()
    endpoint = urlsplit(value['webSocketDebuggerUrl'])
    if (endpoint.scheme != 'ws' or endpoint.hostname not in ('127.0.0.1', 'localhost') or endpoint.username
            or endpoint.password
            or endpoint.port != port or endpoint.query or endpoint.fragment
            or not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', endpoint.path)):
        raise ValueError('Native browser CDP identity is invalid')
    return endpoint.path


def _owner(profile, pid, generation=None):
    lock = instance._lock(profile)
    owner = instance._process(pid)
    if (not lock or lock['pid'] != pid or not instance._matches(owner, profile)
            or (generation is not None and instance._generation(owner) != generation)):
        raise ValueError('Native browser ownership changed')
    return owner, lock


def _publish(profile, pid, port, *, expected_generation=None, expected_lock=None, missing_only=False):
    owner, lock = _owner(profile, pid, expected_generation)
    if expected_lock is not None and lock != expected_lock:
        raise ValueError('Native browser profile lock changed')
    generation = instance._generation(owner)
    if not instance._owns_listener(owner, port, address='127.0.0.1'):
        raise ValueError('Native browser does not own CDP listener')
    endpoint = _endpoint(port)
    if _owner(profile, pid, generation)[1] != lock:
        raise ValueError('Native browser profile lock changed')
    descriptor, temporary = tempfile.mkstemp(prefix='.DevToolsActivePort-', dir=profile)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(f'{port}\n{endpoint}\n')
            output.flush()
            os.fsync(output.fileno())
        if (_owner(profile, pid, generation)[1] != lock
                or not instance._owns_listener(owner, port, address='127.0.0.1')):
            raise ValueError('Native browser generation changed before publication')
        if missing_only:
            os.link(temporary, profile / 'DevToolsActivePort', follow_symlinks=False)
        else:
            os.replace(temporary, profile / 'DevToolsActivePort')
    finally:
        Path(temporary).unlink(missing_ok=True)
    # The same read-only admission guard remains authoritative after publication.
    return instance.inspect_profile(profile)


def _recover_existing_cdp(profile):
    """Republish only a verified live owner's declared fixed loopback CDP listener."""
    lock = instance._lock(profile)
    if lock is None:
        return None
    owner, lock = _owner(profile, lock['pid'])
    value = instance._flag(owner['argv'], '--remote-debugging-port')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value) or not 0 < int(value) < 65536:
        return None
    return _publish(profile, owner['pid'], int(value), expected_generation=instance._generation(owner),
                    expected_lock=lock, missing_only=True)


def _wait_ready(profile, process, port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Native browser exited before CDP admission')
        try:
            result = _publish(profile, process.pid, port)
            if result.get('state') == 'attachable' and result.get('port') == port:
                return result
        except (OSError, ValueError, KeyError, IndexError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    raise RuntimeError('Native browser did not publish a verified CDP listener')


@contextlib.contextmanager
def _fence(profile, timeout):
    import fcntl
    descriptor = os.open(profile / '.hermes-launch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a+', encoding='utf-8') as fence:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Native browser launch admission is busy') from None
                time.sleep(0.1)
        yield


def refresh_dock(config_home, exec_line):
    """Refresh only Hermes' Browser entry, without restarting a desktop or browser."""
    directory = Path(config_home) / 'xfce4/panel'
    for path in directory.glob('launcher-*/hermes.desktop'):
        if path.is_symlink() or not path.is_file():
            continue
        lines = path.read_text(encoding='utf-8-sig').splitlines()
        if 'Name=Browser' not in lines:
            continue
        value = '\n'.join(exec_line if line.startswith('Exec=') else line for line in lines) + '\n'
        if value != path.read_text(encoding='utf-8-sig'):
            descriptor, temporary = tempfile.mkstemp(prefix='.browser-', dir=path.parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    output.write(value)
                os.chmod(temporary, path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)


def ensure(argv, *, exclude_session=None, timeout=15, open_google=False):
    """Adopt an owned endpoint or launch once. Never unlink/kill a profile owner."""
    profile_value = instance._flag(argv, '--user-data-dir')
    if not profile_value or not os.path.isabs(profile_value):
        raise ValueError('Native desktop browser requires its absolute profile path')
    profile = Path(profile_value)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _fence(profile, timeout):
        result = instance.inspect_profile(profile, exclude_session=exclude_session)
        # Human singleton forwarding needs a verified owner, not its agent-only CDP endpoint.
        if open_google and result['state'] in ('attachable', 'owned', 'blocked'):
            return _forward_google(argv, profile)
        if result['state'] == 'blocked' and not os.path.lexists(profile / 'DevToolsActivePort'):
            try:
                recovered = _recover_existing_cdp(profile)
                if recovered is not None and recovered.get('state') == 'attachable':
                    result = recovered
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException):
                pass  # Failed proof preserves the original refusal; it never grants a new launch.
        instance.admitted_port(result)  # Agent attachment still refuses blocked/unknown owners.
        if result['state'] in ('attachable', 'owned'):
            return result
        port = _positive_port()  # executed HERE, in the browser's user/network namespace.
        command = [*argv, '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                   *(['https://accounts.google.com/'] if open_google else [])]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        result = _wait_ready(profile, process, port, timeout)
        return {'opened': True} if open_google else result


def _forward_google(argv, profile):
    """Chrome singleton forwarding inside the existing launch fence; no extra persistent instance."""
    lock = instance._lock(profile)
    if lock is None:
        raise ValueError('Native browser owner is unavailable')
    owner, _ = _owner(profile, lock['pid'])
    generation = instance._generation(owner)
    try:
        result = subprocess.run([*argv, '--new-tab', 'https://accounts.google.com/'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Native browser address outcome is unknown') from None
    if result.returncode:
        raise RuntimeError('Native browser did not accept the address')
    _owner(profile, lock['pid'], generation)
    return {'opened': True}


def open_google(argv):
    return ensure(argv, open_google=True)


def dock(argv):
    """A human click is an explicit request, with the same image-owned pressure gate."""
    if argv and argv[0] == '--guard':
        guard = json.loads(argv[1])
        argv = argv[2:]
        # Only the installed own wrapper may request a permit for its own state.
        state = Path(guard['state'])
        executable = Path(guard['executable'])
        script = Path(guard['script'])
        if (not argv or argv[0] != str(executable) or executable.parent != state
                or executable.name != 'chrome' or not state.is_absolute() or not script.is_absolute()
                or state.stat().st_uid != os.getuid() or script.stat().st_uid != 0  # windows-footgun: ok — Unix desktop UID
                or script.name != 'browser_watchdog.py' or script.is_symlink()):
            raise ValueError('Native dock watchdog identity is invalid')
        result = subprocess.run(['python3', str(script), 'permit', '--state', str(state)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode:
            raise RuntimeError('Native dock browser lacks memory headroom')
    return ensure(argv)


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == '--refresh-dock':
            request = json.load(sys.stdin)
            refresh_dock(request['config_home'], request['exec_line'])
            result = {'refreshed': True}
        elif len(sys.argv) > 1 and sys.argv[1] == '--dock':
            result = dock(sys.argv[2:])
        else:
            request = json.load(sys.stdin)
            result = open_google(request['argv']) if request.get('open_google') is True else ensure(request['argv'], exclude_session=request.get('exclude_session'))
        print(json.dumps(result))
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        # No argv, endpoint, profile contents or provider data in a tool-visible error.
        state = error.state if isinstance(error, instance.ProfileUnavailable) else 'unknown'
        print(json.dumps({'state': state}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
