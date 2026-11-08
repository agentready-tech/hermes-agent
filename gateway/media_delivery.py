"""Prepare explicitly requested native media from one selected own source, never host fallback."""
from contextlib import contextmanager
import hashlib
import os
import json
from pathlib import Path, PurePosixPath
import shlex
import tempfile
from typing import Optional

MAX_FILE = 10 * 1024 * 1024
MAX_BATCH = 20 * 1024 * 1024
MAX_FILES = 5

# This probe runs inside the selected backend, before and after its existing fetch_file.
# It emits identities/hashes only; paths or exception diagnostics never become chat output.
_PROBE = r'''
import hashlib,json,os,pathlib,stat,sys
try:
 p=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); limit=int(sys.argv[3])
 if not p.is_absolute() or not root.is_absolute() or '..' in p.parts: raise ValueError()
 p.relative_to(root)
 denied={'.ssh','.gnupg','.config','.agentsee','browser-profile'}
 for part in p.parts:
  low=part.lower()
  if low in denied or low.startswith('.env') or low in {'.netrc','.pgpass','.npmrc','.pypirc','.git-credentials'}: raise ValueError()
 for parent in [*reversed(p.parents),p]:
  if parent.is_symlink(): raise ValueError()
 fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno())
  if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or not 0<s.st_size<=limit: raise ValueError()
  h=hashlib.sha256(); count=0
  while chunk:=f.read(1024*1024):
   count+=len(chunk)
   if count>limit: raise ValueError()
   h.update(chunk)
  t=os.fstat(f.fileno())
  if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)!=(t.st_dev,t.st_ino,t.st_size,t.st_mtime_ns) or count!=s.st_size: raise ValueError()
  print(json.dumps({'identity':[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns],'sha256':h.hexdigest(),'size':count}))
except (OSError,ValueError):
 sys.exit(1)
'''


def _probe(environment, path, root, max_file):
    command = 'python3 -I -B -c ' + shlex.quote(_PROBE) + ' ' + ' '.join(
        shlex.quote(str(value)) for value in (path, root, max_file))
    result = environment.execute(command, timeout=30)
    if result.get('returncode') != 0 or len(result.get('output', '')) > 1024:
        raise ValueError('Attachment source unavailable')
    try:
        value = json.loads(result['output'])
        if set(value) != {'identity', 'sha256', 'size'} or not 0 < value['size'] <= max_file:
            raise ValueError()
        return value
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Attachment source unavailable') from exc


@contextmanager
def own_home(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@contextmanager
def producing_environment(home, route, task_id=None):
    """Use the profile's configured backend and exact session route; never probe another source."""
    from gateway.session_context import _SESSION_KEY, _SESSION_PROFILE
    from hermes_constants import profile_name_for_home
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_lifecycle import ensure_task_env
    # Pin and restore the existing task-local selectors; never inherit another route's
    # process environment or change global session variables while selecting the backend.
    key = _SESSION_KEY.set(route or '')
    profile = _SESSION_PROFILE.set(profile_name_for_home(Path(home)) or '')
    try:
        with own_home(home), install_and_reset_profile_terminal_scope(home):
            config = _get_env_config()
            if config['env_type'] == 'local':
                yield None, None
            else:
                if not isinstance(route, str) or not route:
                    raise ValueError('Attachment producing session unavailable')
                environment = ensure_task_env(task_id or route)
                if environment is None:
                    raise ValueError('Attachment backend unavailable')
                if config['env_type'] == 'kubernetes' and any(
                        getattr(environment, field, None) != config['kubernetes_' + field]
                        for field in ('namespace', 'pod', 'container')):
                    raise ValueError('Attachment backend ownership mismatch')
                yield environment, str(environment.cwd)
    finally:
        _SESSION_PROFILE.reset(profile)
        _SESSION_KEY.reset(key)


def extract_files(paths, destination, environment, root, *, max_file=MAX_FILE, max_batch=MAX_BATCH, max_files=MAX_FILES):
    """All files must verify before the batch can be advertised; no ambient filesystem lookup."""
    if not 1 <= len(paths) <= max_files:
        raise ValueError('Attachment limit exceeded')
    extracted, total = [], 0
    for index, value in enumerate(paths):
        path = PurePosixPath(value)
        if not path.is_absolute():
            path = PurePosixPath(root) / path
        if '..' in path.parts or not path.name or len(path.name) > 180 or any(ord(c) < 32 or c in '\\/' for c in path.name):
            raise ValueError('Invalid attachment')
        before = _probe(environment, path, root, max_file)
        total += before['size']
        if total > max_batch:
            raise ValueError('Attachment limit exceeded')
        folder = destination / str(index)
        folder.mkdir()
        target = folder / path.name
        environment.fetch_file(str(path), target, max_bytes=max_file)
        # Hash the extracted bytes and recheck the producer's same file identity after transfer.
        if (target.stat().st_size != before['size'] or hashlib.sha256(target.read_bytes()).hexdigest() != before['sha256']
                or _probe(environment, path, root, max_file) != before):
            raise ValueError('Attachment changed during transfer')
        extracted.append(target)
    return extracted



def native_artifact_roots(home):
    """Own workspace/scratch plus the existing first-party artifact roots; never other profiles."""
    from gateway.platforms.base import MEDIA_DELIVERY_SAFE_ROOTS, _MEDIA_DELIVERY_CACHE_SUBDIRS
    home = Path(home)
    return [home / 'workspace', home / 'cache' / 'scratch',
            *(home / 'cache' / part for part in _MEDIA_DELIVERY_CACHE_SUBDIRS),
            *(Path(root) for root in MEDIA_DELIVERY_SAFE_ROOTS if Path(root).is_relative_to(home))]


def native_artifact(path, home):
    """The namespace selects the source before existence checks; absence never changes it."""
    value = Path(path)
    return any(value == root or root in value.parents for root in native_artifact_roots(home))


def _native_probe(path, max_file, *, authorized_namespace=False, owner_uid=None):
    from agent.file_safety import get_read_block_error
    from gateway.platforms.base import validate_media_delivery_path, _ROOT_CREDENTIAL_PATHS
    from gateway.session_ingress_media import _open_regular
    if (not path.is_absolute() or path.resolve() != path or any(parent.is_symlink() for parent in path.parents)
            or path.name.lower().startswith('.env') or get_read_block_error(str(path))):
        raise ValueError('Attachment source unavailable')
    # Explicitly authorized artifact inputs may be outside strict delivery roots.
    # Secret/history/config identities are screened BEFORE they can become retained artifacts.
    if authorized_namespace:
        protected = {Path(item).name.lower() for item in _ROOT_CREDENTIAL_PATHS}
        if any(part.lower() in protected for part in path.parts):
            raise ValueError('Attachment source denied')
    else:
        safe = validate_media_delivery_path(str(path))
        if safe is None or Path(safe) != path:
            raise ValueError('Attachment source denied')
    with _open_regular(path) as source:
        before = os.fstat(source.fileno())
        if (before.st_nlink != 1 or not 0 < before.st_size <= max_file
                or (authorized_namespace and before.st_uid != owner_uid)):
            raise ValueError('Attachment limit exceeded')
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > max_file:
                raise ValueError('Attachment limit exceeded')
            digest.update(chunk)
        after = os.fstat(source.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
    identity = tuple(getattr(before, field) for field in fields)
    if identity != tuple(getattr(after, field) for field in fields) or size != before.st_size:
        raise ValueError('Attachment changed during transfer')
    return {'identity': identity, 'size': size, 'sha256': digest.hexdigest()}



def _retained_reference(path, max_file):
    from gateway.session_ingress_media import _media_root, restore_native_media
    if path.parent.parent != _media_root() or len(path.parent.name) != 64:
        return None
    if any(char not in '0123456789abcdef' for char in path.parent.name):
        return None
    size = path.stat().st_size
    if not 0 < size <= max_file:
        raise ValueError('Attachment limit exceeded')
    reference = {'path': str(path), 'sha256': path.parent.name, 'size': size}
    restore_native_media([reference])
    return reference


def prepare_files(paths, home, route, *, task_id=None, max_file=MAX_FILE, max_batch=MAX_BATCH, max_files=MAX_FILES):
    """One deterministic stager for native-channel senders and Panel's committed-row descriptors.

    Explicit own-home artifact namespaces choose native bytes. Other paths choose only the
    configured own terminal backend. Both use the existing capture/restore implementation.
    """
    from contextlib import ExitStack
    from gateway.media_fetch import remote_path_is_denied
    from gateway.session_ingress_media import capture_native_media, restore_native_media
    if not 1 <= len(paths) <= max_files:
        raise ValueError('Attachment limit exceeded')
    from hermes_constants import get_hermes_dir
    home = Path(home)
    if home.resolve() != home or any((home / relative).is_symlink() for relative in
                                    ('cache', 'cache/documents', 'document_cache')):
        raise ValueError('Attachment retained namespace unavailable')
    retained_root = get_hermes_dir('cache/documents', 'document_cache', home=home) / 'native-inputs'
    if retained_root.resolve() != retained_root:
        raise ValueError('Attachment retained namespace unavailable')
    with own_home(home), ExitStack() as stack:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='native-file-')))
        selected = None
        local, expected, retained, origins, total = [], [], {}, [], 0
        owner_uid = Path(home).stat().st_uid
        for index, raw in enumerate(paths):
            path = Path(raw)
            if (not path.is_absolute() or '..' in path.parts or not path.name or len(path.name) > 180
                    or any(ord(char) < 32 or char in '/\\' for char in path.name)):
                raise ValueError('Invalid attachment source')
            if native_artifact(path, home):
                before = _native_probe(path, max_file, authorized_namespace=True, owner_uid=owner_uid)
                local.append(path)
                expected.append((path, before, True))
                origins.append({'kind': 'native-artifact', 'home': str(home), 'task_id': task_id or route, 'backend': 'native', 'path': str(path)})
                if reference := _retained_reference(path, max_file):
                    retained[index] = reference
            else:
                if selected is None:
                    selected = stack.enter_context(producing_environment(home, route, task_id))
                environment, _cwd = selected
                if environment is None:
                    before = _native_probe(path, max_file)
                    local.append(path)
                    expected.append((path, before, False))
                    origins.append({'kind': 'terminal-backend', 'home': str(home), 'task_id': task_id or route, 'backend': 'local', 'path': str(path)})
                else:
                    from gateway.media_policy import media_delivery_strict
                    if media_delivery_strict():
                        raise ValueError('Attachment source denied')
                    if remote_path_is_denied(str(path), getattr(environment, '_remote_home', None)):
                        raise ValueError('Attachment source denied')
                    folder = directory / str(index)
                    folder.mkdir()
                    copied = extract_files([str(path)], folder, environment, '/', max_file=max_file,
                                           max_batch=max_batch - total, max_files=1)[0]
                    before = {'size': copied.stat().st_size, 'sha256': hashlib.sha256(copied.read_bytes()).hexdigest()}
                    local.append(copied)
                    expected.append((None, before, False))
                    origins.append({'kind': 'terminal-backend', 'home': str(home), 'task_id': task_id or route,
                                    'backend': type(environment).__name__, 'path': str(path)})
            total += before['size']
            if total > max_batch:
                raise ValueError('Attachment limit exceeded')
        captured = iter(capture_native_media([path for index, path in enumerate(local) if index not in retained]))
        references = [retained[index] if index in retained else next(captured) for index in range(len(local))]
        for reference, (original, before, authorized) in zip(references, expected):
            if (reference['size'] != before['size'] or reference['sha256'] != before['sha256']
                    or (original is not None and _native_probe(original, max_file, authorized_namespace=authorized, owner_uid=owner_uid) != before)):
                raise ValueError('Attachment changed during transfer')
        # Verify the actual retained bytes before any adapter or viewer sees success.
        if any(Path(ref['path']).parent.parent != retained_root for ref in references):
            raise ValueError('Attachment retained namespace mismatch')
        restore_native_media(references)
        from gateway.platforms.base import validate_media_delivery_path
        if any(validate_media_delivery_path(ref['path']) != ref['path'] for ref in references):
            raise ValueError('Attachment staged output denied')
        return [{'name': path.name, 'reference': ref, 'origin': origin}
                for path, ref, origin in zip(local, references, origins)]


def prepare_requested_media(raw, session_key=''):
    """Managed MEDIA-only edge; original sender owns the existing platform upload and result."""
    from gateway.session_context import get_session_env
    from hermes_runtime.adapters.hermes import managed_home
    from gateway.media_fetch import _FETCH_MAX_BYTES
    route = session_key or get_session_env('HERMES_SESSION_KEY')
    try:
        prepared = prepare_files([raw], managed_home(), route, task_id=get_session_env('HERMES_SESSION_ID'), max_file=_FETCH_MAX_BYTES,
                                 max_batch=_FETCH_MAX_BYTES, max_files=1)
        return prepared[0]['reference']['path']
    except (OSError, ValueError, RuntimeError):
        return None


def _validated_delivery_path(raw_path, session_key: str, label: str,
                             dropped: Optional[list[dict]] = None, *, explicit: bool = False) -> Optional[str]:
    """``validate_media_delivery_path`` plus the shared "Skipping unsafe ..." warning. A path the
    host cannot see is retried against the active remote sandbox (ssh/modal/...; #466). When
    ``dropped`` is a list, a rejected path is appended as ``{"path", "reason"}`` so the caller can
    report the drop instead of booking a delivery that never happened (#115908)."""
    from gateway.platforms.base import (
        _existing_regular_file, _log_safe_path, logger, validate_media_delivery_path)
    raw = str(raw_path)
    from hermes_runtime.adapters.hermes import enabled
    managed = explicit and enabled()
    if managed:
        safe_path = prepare_requested_media(raw, session_key)
    else:
        safe_path = validate_media_delivery_path(raw, session_key=session_key)
        if not safe_path:
            from gateway.media_fetch import fetch_remote_media
            safe_path = fetch_remote_media(raw)
    if not safe_path:
        # Say WHY: a path that does not exist on the host is the common case (a model hallucinated or
        # a sandbox path failed to translate) and is not a security rejection.
        reason = ("requested artifact unavailable from its selected source" if managed else
                  "not found on this host" if not _existing_regular_file(raw) else "denied by the delivery policy")
        logger.warning("Skipping %s (%s): %s", label, reason, _log_safe_path(raw))
        if dropped is not None:
            dropped.append({"path": raw, "reason": reason})
    return safe_path
