"""Read-only shared-profile admission, also executed in the sandbox's PID namespace.

No endpoint contents, process argv or environment leave this probe. A missing
DevTools file never turns a live or unverifiable profile owner into an absent one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import sys

_BROWSERS = {'chrome', 'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable',
             'Google Chrome', 'Google Chrome for Testing', 'Chromium', 'chrome-headless-shell'}


class ProfileUnavailable(RuntimeError):
    def __init__(self, state):
        self.state = state if state in ('blocked', 'unknown') else 'unknown'
        super().__init__('The shared browser profile is occupied or its ownership cannot be verified. '
                         'No second Chrome was started; do not delete profile locks or retry an unsafe action.')


def admitted_port(result):
    state = result.get('state')
    if state == 'attachable' and type(result.get('port')) is int and 0 < result['port'] < 65536:
        return result['port']
    if state in ('absent', 'stale', 'owned'):
        return None
    raise ProfileUnavailable(state or 'unknown')


def _argv(raw):
    values = [part.decode('utf-8', 'replace') for part in raw.split(bytes([0])) if part]
    return shlex.split(values[0]) if len(values) == 1 else values


def _flag(argv, name):
    for index, value in enumerate(argv):
        if value.startswith(name + '='):
            return value.partition('=')[2]
        if value == name:
            return argv[index + 1] if index + 1 < len(argv) else ''
    return None


def _uid():
    getuid = getattr(os, 'getuid', None)
    if getuid is None:
        raise OSError('Shared desktop profile identity requires a Unix user')
    return getuid()


def _linux_process(pid):
    process = Path('/proc', str(pid))
    before = (process / 'stat').read_text(encoding='utf-8-sig').rpartition(')')[2].split()
    status = dict(line.split(':', 1) for line in (process / 'status').read_text(encoding='utf-8-sig').splitlines() if ':' in line)
    result = {'pid': pid, 'start': before[19], 'uid': int(status['Uid'].split()[0]),
              'exe': os.readlink(process / 'exe'), 'argv': _argv((process / 'cmdline').read_bytes()),
              'cgroup': (process / 'cgroup').read_text(encoding='utf-8-sig'), 'parent': int(before[1]),
              'boot': Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8-sig').strip()}
    after = (process / 'stat').read_text(encoding='utf-8-sig').rpartition(')')[2].split()
    if before[19] != after[19] or before[0] == 'Z':
        raise ProcessLookupError(pid)
    return result


def _process(pid):
    if Path('/proc/self/stat').exists():
        return _linux_process(pid)
    import psutil
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            result = {'pid': pid, 'start': process.create_time(), 'uid': process.uids().real,
                      'exe': process.exe(), 'argv': process.cmdline(), 'cgroup': None,
                      'parent': process.ppid(), 'boot': psutil.boot_time()}
        if process.create_time() != result['start']:
            raise ProcessLookupError(pid)
    except psutil.NoSuchProcess as error:
        raise ProcessLookupError(pid) from error
    except psutil.AccessDenied as error:
        raise PermissionError(pid) from error
    return result


def _owner_session(owner):
    try:
        parent = _process(owner['parent'])
        if parent['uid'] != owner['uid'] or parent['cgroup'] != owner['cgroup']:
            return None
        if Path('/proc/self/stat').exists():
            raw = Path('/proc', str(parent['pid']), 'environ').read_bytes()
            marker = next((value.partition(b'=')[2].decode() for value in raw.split(bytes([0]))
                           if value.startswith(b'AGENT_BROWSER_SESSION=')), None)
        else:
            import psutil
            try:
                marker = psutil.Process(parent['pid']).environ().get('AGENT_BROWSER_SESSION')
            except psutil.Error:
                return None
        return marker if _process(parent['pid'])['start'] == parent['start'] else None
    except (OSError, ValueError):
        return None  # Missing parent proof never grants an own-daemon exemption.


def _matches(process, profile):
    expected_group = Path('/proc/self/cgroup').read_text(encoding='utf-8-sig') if process['cgroup'] is not None else None
    return (process['uid'] == _uid() and Path(process['exe']).name in _BROWSERS
            and process['cgroup'] == expected_group and _flag(process['argv'], '--type') is None
            and _flag(process['argv'], '--user-data-dir') == str(profile))


def _candidates():
    if Path('/proc/self/stat').exists():
        names = {name[:15] for name in _BROWSERS}
        for path in Path('/proc').iterdir():
            try:
                if path.name.isdigit() and (path / 'comm').read_text(encoding='utf-8-sig').strip() in names:
                    yield int(path.name)
            except OSError:
                continue
        return
    import psutil
    for process in psutil.process_iter(['pid', 'name']):
        if process.info['name'] in _BROWSERS:
            yield process.pid


def _other_owner(profile):
    """Names only narrow the scan; exe/UID/cgroup/profile still prove ownership."""
    for pid in _candidates():
        try:
            if _matches(_process(pid), profile):
                return True
        except PermissionError:
            return True  # An unreadable Chrome candidate is never proof of absence.
        except (OSError, ValueError, IndexError):
            continue
    return False


def _lock(profile):
    path = profile / 'SingletonLock'
    try:
        value = path.lstat()
        host, separator, pid = os.readlink(path).rpartition('-')
    except FileNotFoundError:
        return None
    if (not separator or host != socket.gethostname() or not pid.isdigit()
            or value.st_uid != _uid() or int(pid) < 1):
        raise ValueError('unverified profile lock')
    return {'pid': int(pid), 'identity': (value.st_dev, value.st_ino, value.st_ctime_ns)}


def _owns_listener(owner, port):
    if not Path('/proc/self/stat').exists():
        import psutil
        try:
            return any(row.status == 'LISTEN' and row.laddr.port == port
                       for row in psutil.Process(owner['pid']).net_connections(kind='tcp'))
        except psutil.Error:
            return False
    inodes = set()
    for table in ('tcp', 'tcp6'):
        for line in Path('/proc/net', table).read_text(encoding='utf-8-sig').splitlines()[1:]:
            values = line.split()
            if values[3] == '0A' and int(values[1].rpartition(':')[2], 16) == port:
                inodes.add('socket:[' + values[9] + ']')
    for path in Path('/proc', str(owner['pid']), 'fd').iterdir():
        try:
            if os.readlink(path) in inodes:
                return True
        except FileNotFoundError:
            continue
    return False


def _generation(process):
    return tuple(process[key] for key in ('pid', 'start', 'boot', 'uid', 'cgroup', 'exe'))


def _port(profile, owner):
    value = int((profile / 'DevToolsActivePort').read_text(encoding='utf-8-sig').splitlines()[0])
    if not 0 < value < 65536:
        raise ValueError('invalid port')
    if not _owns_listener(owner, value):
        raise ValueError('port does not belong to profile owner')
    with socket.create_connection(('127.0.0.1', value), timeout=0.5):
        return value


def inspect_profile(profile, *, exclude_session=None):
    profile = Path(profile)
    try:
        lock = _lock(profile)
        if lock is None:
            return {'state': 'unknown' if _other_owner(profile) else 'absent'}
        try:
            owner = _process(lock['pid'])
        except (ProcessLookupError, FileNotFoundError):
            return {'state': 'unknown' if _other_owner(profile) else 'stale'}
        if not _matches(owner, profile):
            return {'state': 'unknown'}
        if exclude_session and _owner_session(owner) == exclude_session:
            state = {'state': 'owned'}
        else:
            try:
                state = {'state': 'attachable', 'port': _port(profile, owner)}
            except (OSError, ValueError, IndexError):
                state = {'state': 'blocked'}
        if _lock(profile) != lock or _generation(_process(lock['pid'])) != _generation(owner):
            return {'state': 'unknown'}
        return state
    except (OSError, ValueError, IndexError):
        return {'state': 'unknown'}


if __name__ == '__main__':
    print(json.dumps(inspect_profile(sys.argv[1], exclude_session=sys.argv[2] or None)))
