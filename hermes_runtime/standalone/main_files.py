"""Panel files are retained bytes bound to an actual settled main-conversation row."""
import base64
from contextlib import contextmanager
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import secrets
import sqlite3

from gateway.media_delivery import MAX_BATCH, MAX_FILE, MAX_FILES

CAPABILITY = 'chat-files-v1'
KEY = 'panel_files_v1'
ERROR = 'Не удалось прикрепить файл. Повторите отправку.'
_ERRORS = (ERROR, 'Не удалось прикрепить файл. Проверьте файл в рабочей папке и повторите отправку.')
HINT = (
    '## Panel file delivery\n'
    'This AgentReady main supports chat-files-v1. To send a generated file, include '
    'MEDIA:/absolute/path/to/file in your final reply, outside code/quotes. A bare path does not '
    'send a file. The system stages explicitly requested files from your own native artifact '
    'namespace or producing backend; these filesystems are distinct. Only verified retained files '
    'become downloadable cards. Panel limits: 5 files, 10 MiB each, 20 MiB total. Never export credentials, '
    'browser profiles or private runtime files; report a delivery error honestly.'
)


@contextmanager
def own_home(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def public_files(marker, row_id):
    """No retained path can leave this projection, even for malformed stored metadata."""
    if not isinstance(marker, dict) or set(marker) - {'files', 'error'}:
        return []
    files = marker.get('files', [])
    if not isinstance(files, list) or len(files) > MAX_FILES:
        return []
    projected, total, ids = [], 0, set()
    for item in files:
        if (not isinstance(item, dict) or set(item) != {'id','row_id','name','mime','size','sha256','reference','origin'}
                or type(row_id) is not int or row_id < 1 or item['row_id'] != row_id
                or not isinstance(item['id'], str) or not re.fullmatch(r'att-[a-f0-9]{32}', item['id'])
                or item['id'] in ids or type(item['size']) is not int or not 0 < item['size'] <= MAX_FILE
                or not isinstance(item['name'], str) or not 1 <= len(item['name']) <= 180
                or any(ord(c) < 32 or c in '/\\' for c in item['name'])
                or not isinstance(item['mime'], str) or not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', item['mime'])
                or not isinstance(item['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
            return []
        total += item['size']
        ids.add(item['id'])
        projected.append({key: item[key] for key in ('id', 'row_id', 'name', 'mime', 'size', 'sha256')})
    return projected if total <= MAX_BATCH else []


def project_row(row):
    from gateway.platforms.base import BasePlatformAdapter
    metadata = row.get('display_metadata') or {}
    marker = metadata.get(KEY) if isinstance(metadata, dict) else None
    if row.get('role') != 'assistant' or marker is None:
        return row
    result = {**row, 'display_metadata': {k: v for k, v in metadata.items() if k != KEY}}
    for key in ('content', 'text'):
        if isinstance(result.get(key), str):
            result[key] = BasePlatformAdapter.strip_media_directives_for_display(result[key])
    result['attachments'] = public_files(marker, row.get('row_id'))
    if isinstance(marker, dict) and marker.get('error') in _ERRORS:
        result['attachment_error'] = ERROR
    return result


def _stored_row(db, target, row_id):
    lineage = db._resume_lineage_ids(target)
    placeholders = ','.join('?' for _ in lineage)
    with db._read_ctx() as conn:
        row = conn.execute(f'SELECT role,content,display_metadata,session_id FROM messages WHERE id=? AND session_id IN ({placeholders}) '
                           'AND (active=1 OR compacted=1)', (row_id, *lineage)).fetchone()
    if row is None or row[0] != 'assistant' or not isinstance(row[1], str):
        raise ValueError('Attachment row unavailable')
    return row[1], json.loads(row[2]) if row[2] else {}, row[3]


def retain_row(db, target, row_id, home, route, delivery_text=None):
    """Select by the committed turn receipt, never by equal body text or a caller-supplied path."""
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.media_delivery import prepare_files
    content, metadata, task_id = _stored_row(db, target, row_id)
    if KEY in metadata:
        return metadata[KEY]
    paths, _ = BasePlatformAdapter.extract_media(delivery_text if isinstance(delivery_text, str) else content)
    if not paths:
        return None
    marker = {'files': []}
    try:
        prepared = prepare_files([path for path, _voice in paths], home, route, task_id=task_id)
        marker['files'] = [dict(id='att-' + secrets.token_hex(16), row_id=row_id, name=item['name'],
            mime=mimetypes.guess_type(item['name'])[0] or 'application/octet-stream', size=item['reference']['size'],
            sha256=item['reference']['sha256'], reference=item['reference'], origin=item['origin']) for item in prepared]
    except (OSError, ValueError, RuntimeError):
        marker['error'] = ERROR
    # Re-read and merge under the existing store writer: preserve reactions and idempotent receipts.
    def save(conn):
        current = conn.execute('SELECT content,display_metadata FROM messages WHERE id=?', (row_id,)).fetchone()
        if current is None or current[0] != content:
            raise ValueError('Attachment row changed')
        saved = json.loads(current[1]) if current[1] else {}
        if KEY not in saved:
            saved[KEY] = marker
            conn.execute('UPDATE messages SET display_metadata=? WHERE id=?', (json.dumps(saved), row_id))
        return saved[KEY]
    return db._execute_write(save)


def settle_files(authority, live, ref, settled, captured, response=None):
    """Only the bound managed-main owner exports; no I/O while its publication lock is held."""
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.adapters.state import resolve_home
    if (not enabled() or Path(authority.profile_id).resolve() != managed_home()
            or resolve_home(ref.session_id) is None or settled.get('outcome') != 'completed'):
        return
    result = (captured or {}).get('result') or {}
    row_id = (result.get('persisted_turn') or {}).get('final_assistant_row_id')
    if type(row_id) is not int or row_id < 1:
        return
    from gateway.session_ancillary import physical_target
    try:
        marker = retain_row(authority.db, physical_target(authority, ref), row_id, managed_home(), live.route, response)
    except (OSError, ValueError, sqlite3.Error):
        marker = {'files': [], 'error': ERROR}
    if marker is not None:
        result['panel_files'] = {'attachments': public_files(marker, row_id),
            **({'attachment_error': ERROR} if marker.get('error') in _ERRORS else {})}

        # Delivery-only rewrite: TG and Panel use the same immutable references. The stored
        # assistant body/api_content and the model's historical prompt are never rewritten.
        from gateway.platforms.base import BasePlatformAdapter
        _paths, body = BasePlatformAdapter.extract_media(response or '')
        if marker.get('error') in _ERRORS:
            # Panel's 10/20 MiB limits must not suppress an otherwise valid native
            # platform send (Telegram's existing preparation still allows 50 MiB).
            return None
        media = ['MEDIA:' + item['reference']['path'] for item in marker.get('files', [])]
        flags = [flag for flag in ('[[audio_as_voice]]', '[[as_document]]') if flag in (response or '')]
        return '\n'.join(part for part in (body, *flags, *media) if part)


def download(rows, identity, home):
    """A token authorizes nothing without a published row in this authenticated own snapshot."""
    from gateway.session_ingress_media import restore_native_media, _open_regular
    if not isinstance(identity, str) or not re.fullmatch(r'att-[a-f0-9]{32}', identity):
        raise ValueError('Attachment unavailable')
    matches = []
    for row in rows:
        if row.get('role') != 'assistant':
            continue
        metadata = row.get('display_metadata')
        marker = metadata.get(KEY) if isinstance(metadata, dict) else None
        if not isinstance(marker, dict) or not public_files(marker, row.get('row_id')):
            continue
        for item in marker['files']:
            if item['id'] == identity:
                matches.append(item)
    if len(matches) != 1:
        raise ValueError('Attachment unavailable')
    item = matches[0]
    with own_home(home):
        paths = restore_native_media([item['reference']])
        with _open_regular(Path(paths[0])) as source:
            data = source.read(MAX_FILE + 1)
    if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Attachment unavailable')
    return {**{key: item[key] for key in ('id', 'row_id', 'name', 'mime', 'size', 'sha256')},
            'data_base64': base64.b64encode(data).decode('ascii')}
