"""Bounded, read-only archive pages for the managed Panel conversation."""

import asyncio
import json

from hermes_state_runtime import RuntimeStoreError

PAGE_LIMIT = 40
PAGE_BYTES = 175_000
_MAX_LIMIT = 100
_MAX_SCAN = 160
_TOOL_LOOKBEHIND = 32
_MAX_CURSOR = 2**53 - 1  # Exact JS integer on the Panel wire.
_REASONING_KEYS = ('reasoning', 'reasoning_content', 'reasoning_details',
                   'codex_reasoning_items', 'codex_message_items', 'display_reasoning')


def require_history_owner(connection, ref):
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.adapters.main_transport import require_owner
    from hermes_runtime.sessions import HOME_SESSION_ID

    if not enabled() or ref.session_id != HOME_SESSION_ID:
        raise RuntimeStoreError('permission_denied')
    require_owner(connection)
    connection.authority.authorize(connection.actor, ref, 'session:read')


def _records(db, target, before, scan):
    from hermes_state_messages import DISPLAY_VISIBLE_SQL

    lineage = db._resume_lineage_ids(target)
    marks = ','.join('?' for _ in lineage)
    with db._read_ctx() as conn:
        # Group fixed-width identities before fetching bodies. A pre-index transcript row
        # has its own ordinal; the request does not rewrite the transcript or build an index.
        # A compaction copy keeps the first ordinal even when the active representative changes.
        selected = conn.execute(f'''WITH keyed AS (
            SELECT id,active,CASE WHEN display_identity IS NULL THEN 'row:'||id ELSE hex(display_identity) END AS identity
            FROM messages WHERE session_id IN ({marks}) AND (active=1 OR compacted=1){DISPLAY_VISIBLE_SQL}
        ), groups AS (
            SELECT MIN(id) AS ordinal,COALESCE(MAX(CASE WHEN active=1 THEN id END),MAX(id)) AS representative
            FROM keyed GROUP BY identity
        ), page AS (
            SELECT ordinal,representative FROM groups WHERE ordinal<? ORDER BY ordinal DESC LIMIT ?
        ), bounds AS (
            SELECT COALESCE(MIN(ordinal),0) AS oldest,COALESCE(MAX(ordinal)+1,0) AS total FROM groups
        ) SELECT bounds.oldest,bounds.total,page.ordinal,
                 m.id,m.role,m.timestamp,m.tool_call_id,m.tool_name,m.display_kind,m._compressed_summary,
                 CASE WHEN length(CAST(m.content AS BLOB))<=65536 THEN m.content
                      WHEN typeof(m.content)='text' AND substr(CAST(m.content AS BLOB),1,6)<>X'006a736f6e3a'
                           THEN substr(m.content,1,12000) END AS content,
                 CASE WHEN length(CAST(m.tool_calls AS BLOB))<=65536 THEN m.tool_calls END AS tool_calls,
                 CASE WHEN length(CAST(m.display_metadata AS BLOB))<=32768 THEN m.display_metadata END AS display_metadata,
                 CASE WHEN length(CAST(m.codex_message_items AS BLOB))<=65536 THEN m.codex_message_items END AS codex_message_items
          FROM bounds LEFT JOIN page ON 1=1 LEFT JOIN messages m ON m.id=page.representative
          ORDER BY page.ordinal DESC''',
                                (*lineage, _MAX_CURSOR if before is None else before,
                                 scan + _TOOL_LOOKBEHIND)).fetchall()
    oldest, total = selected[0]['oldest'], selected[0]['total']
    return [(row['ordinal'], row) for row in selected if row['id'] is not None], oldest, total


def history_page(db, target, profile_home, *, before=None, limit=PAGE_LIMIT):
    from hermes_runtime.standalone.main_files import project_row
    from tui_gateway import server

    scan = min(limit * 4, _MAX_SCAN)
    records, oldest, total = _records(db, target, before, scan)
    raw = [dict(db._row_to_message_dict(row, warn_context='Panel archive', summary_flag=True),
                _row_id=row['id'], _panel_history_index=order) for order, row in reversed(records)]
    projected = {}
    for row in server._history_to_messages(raw, profile_home=profile_home, image_urls=False):
        row = project_row({key: value for key, value in row.items() if key not in _REASONING_KEYS})
        if row.get('role') == 'assistant' and not _has_message(row):
            continue
        if len(json.dumps(row, separators=(',', ':'))) > PAGE_BYTES:
            text = row.get('display_content') or row.get('text') or ''
            row = {key: row[key] for key in ('role', 'name', 'history_index', 'row_id', 'timestamp',
                                             'display_kind', 'context', 'tool_call_id') if key in row}
            row['text'] = text[:12000]
        projected[row['history_index']] = row

    page, visible_message, consumed = [], False, min(before, total) if before is not None else total
    remaining_bytes = PAGE_BYTES
    for index, _ in records[:scan]:
        row = projected.get(index)
        if row is not None:
            size = len(json.dumps(row, separators=(',', ':'))) + 1
            if size > remaining_bytes:
                break
            remaining_bytes -= size
            page.append(row)
            visible_message |= _has_message(row)
        consumed = index
        # A nominal page of only tool activity should still make progress into older text.
        if len(page) >= limit and visible_message:
            break
    return {'messages': list(reversed(page)),
            'history': {'native': True, 'total': total, 'before': consumed if oldest < consumed else 0,
                        'has_more': oldest > 0 and oldest < consumed}}


def _has_message(row):
    return (row.get('role') in {'user', 'assistant'}
            and row.get('display_kind') not in {'hidden', 'internal_notification'}
            and (row.get('display_metadata') or {}).get('notification_category') != 'diagnostic'
            and bool(str(row.get('text') or '').strip()
                     or row.get('display_commentary') or row.get('attachments')
                     or row.get('attachment_error')))


async def rpc(connection, ref, params):
    if set(params) - {'session_id', 'before', 'limit'}:
        raise RuntimeStoreError('invalid_params')
    before, limit = params.get('before'), params.get('limit', PAGE_LIMIT)
    if (before is not None and (type(before) is not int or not 0 <= before <= _MAX_CURSOR)
            or type(limit) is not int or not 1 <= limit <= _MAX_LIMIT):
        raise RuntimeStoreError('invalid_params')
    if ref.session_id not in connection.subscriptions:
        raise RuntimeStoreError('permission_denied')
    require_history_owner(connection, ref)
    return await read_page(connection, ref, before=before, limit=limit)


async def read_page(connection, ref, **params):
    authority = connection.authority
    result = await asyncio.to_thread(
        history_page, authority.db, authority.physical_target(ref), authority.profile_id, **params)
    return {'session_id': ref.session_id, **result}
