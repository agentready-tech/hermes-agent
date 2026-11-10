"""Read-only, parent-owned persistent delegation display on the existing main stream."""
import asyncio
from contextlib import contextmanager
import json
import logging
import re
from types import SimpleNamespace

from hermes_state_runtime import RuntimeStoreError

_BUDGET = 180000
logger = logging.getLogger(__name__)
_PROJECT_COLUMNS = 'subagent_id,parent_session_id,project_key,session_id,goal,archived,paused,updated_at'
_VISIBLE_COLUMNS = ("id,role,CASE WHEN typeof(content)='text' AND length(content)<=64000 THEN content END AS content,"
                    "tool_call_id,CASE WHEN length(tool_calls)<=64000 THEN tool_calls END AS tool_calls,tool_name,"
                    "tool_calls IS NOT NULL AND tool_calls!='[]' AS has_tool_calls,"
                    "active,timestamp,display_kind,CASE WHEN length(display_metadata)<=64000 THEN display_metadata END AS display_metadata,"
                    "_compressed_summary,length(content)>64000 OR typeof(content)='blob' AS oversized")


def display_text(value, limit=12000):
    """Only ordinary text parts; never provider reasoning or unredacted credentials."""
    from agent.redact import redact_sensitive_text
    if isinstance(value, list):
        value = '\n'.join(part['text'] for part in value if isinstance(part, dict)
                          and part.get('type') in ('text', 'output_text') and isinstance(part.get('text'), str))
    if not isinstance(value, str):
        return ''
    if value.startswith('\x00json:'):
        return ''
    value = re.sub(r'<(think|thinking|analysis|reasoning)\b[^>]*>.*?(?:</\1>|$)', '', value,
                   flags=re.DOTALL | re.IGNORECASE)
    try:
        return (redact_sensitive_text(value, force=True) or '')[:limit]
    except Exception:  # health: allow BLE001 -- withhold rather than leak if credential redaction fails
        logger.debug('Subagent text redaction unavailable', exc_info=True)
        return ''


def _limit(value, default):
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= 100:
        raise RuntimeStoreError('invalid_params')
    return value


def _before(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise RuntimeStoreError('invalid_params')
    return value


@contextmanager
def _store(connection):
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.adapters.main_transport import require_owner
    from hermes_runtime.sessions import HOME_SESSION_ID
    from tools.delegate_project_store import ProjectStore
    if not enabled():
        raise RuntimeStoreError('permission_denied')
    require_owner(connection)
    authority = connection.authority
    from gateway.session_authorities import require_serving
    require_serving(authority)
    if HOME_SESSION_ID not in connection.subscriptions:
        raise RuntimeStoreError('permission_denied')
    from gateway.session_contract import SessionRef
    ref = SessionRef(authority.profile_id, HOME_SESSION_ID)
    authority.authorize(connection.actor, ref, 'session:read')
    parent = SimpleNamespace(_session_db=authority.db, session_id=authority.physical_target(ref))
    with ProjectStore(parent) as store:
        yield store


def _has_projects(store):
    with store.db._read_ctx() as conn:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delegated_projects'").fetchone() is not None


def _owned(store, identity):
    if not isinstance(identity, str) or not re.fullmatch(r'project-[a-f0-9]{32}', identity):
        raise RuntimeStoreError('invalid_params')
    if not _has_projects(store):
        raise RuntimeStoreError('not_found')
    with store.db._read_ctx() as conn:
        row = conn.execute(f'SELECT {_PROJECT_COLUMNS} FROM delegated_projects WHERE subagent_id=?', (identity,)).fetchone()
    try:
        return store._owned(row)
    except ValueError as exc:
        # Do not disclose whether a foreign parent's child exists.
        raise RuntimeStoreError('not_found') from exc


def _card(store, row):
    project = store._owned(row)
    with store.db._read_ctx() as conn:
        last = conn.execute('SELECT state,CASE WHEN length(result_json)<=64000 THEN result_json END AS result_json '
                            'FROM delegated_project_messages WHERE subagent_id=? '
                            'ORDER BY rowid DESC LIMIT 1', (project['subagent_id'],)).fetchone()
        active = conn.execute("SELECT state FROM delegated_project_messages WHERE subagent_id=? "
                              "AND state IN ('pending','running') ORDER BY rowid LIMIT 1", (project['subagent_id'],)).fetchone()
    status = 'archived' if project['archived'] else 'paused' if project['paused'] else active['state'] if active else 'idle'
    try:
        result = json.loads(last['result_json'] or '{}') if last else {}
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return {'subagent_id': project['subagent_id'], 'name': display_text(project['project_key'], 200),
            'goal': display_text(project['goal'], 2000), 'status': status,
            'last_status': last['state'] if last else None,
            'summary': display_text(result.get('summary') or result.get('error') or '', 4000),
            'updated_at': project['updated_at']}


def _list(connection, params):
    before, limit = params.get('before'), _limit(params.get('limit'), 20)
    if before is not None and (not isinstance(before, str) or not re.fullmatch(r'[01]:[0-9]{1,19}', before)):
        raise RuntimeStoreError('invalid_params')
    tier, cursor = (int(part) for part in before.split(':')) if before else (1, 2**63 - 1)
    if cursor > 2**63 - 1:
        raise RuntimeStoreError('invalid_params')
    tracked = params.get('tracked', [])
    if not isinstance(tracked, list) or len(tracked) > 100 or len(set(filter(lambda x: isinstance(x, str), tracked))) != len(tracked):
        raise RuntimeStoreError('invalid_params')
    with _store(connection) as store:
        states = []
        for identity in tracked:
            card = _card(store, _owned(store, identity))
            states.append({key: card[key] for key in ('subagent_id', 'status', 'last_status', 'updated_at')})
        if not _has_projects(store):
            return {'subagents': [], 'tracked': states, 'page': {'before': '0:0', 'has_more': False}}
        lineage = store.db.get_compression_lineage(store.parent_id) or [store.parent_id]
        marks = ','.join('?' for _ in lineage)
        with store.db._read_ctx() as conn:
            rows = conn.execute(f'WITH owned AS (SELECT rowid AS cursor,{_PROJECT_COLUMNS},EXISTS(SELECT 1 FROM delegated_project_messages m '
                                "WHERE m.subagent_id=delegated_projects.subagent_id AND m.state IN ('pending','running')) AS priority "
                                f'FROM delegated_projects WHERE parent_session_id IN ({marks})) '
                                'SELECT * FROM owned WHERE priority<? OR (priority=? AND cursor<?) '
                                'ORDER BY priority DESC,cursor DESC LIMIT ?',
                                (*lineage, tier, tier, cursor, limit + 1)).fetchall()
        cards, consumed = [], []
        for row in rows[:limit]:
            card = _card(store, row)
            if len(json.dumps({'subagents': cards + [card], 'tracked': states}).encode()) > _BUDGET:
                break
            cards.append(card)
            consumed.append(f"{row['priority']}:{row['cursor']}")
        return {'subagents': cards, 'tracked': states, 'page': {'before': consumed[-1] if consumed else '0:0',
                'has_more': len(rows) > len(cards)}}


def _history_row(row):
    if row.get('display_kind') in ('hidden', 'agent_message', 'internal_notification'):
        return []
    role = row.get('role')
    if role not in ('tool', 'assistant') or row.get('_compressed_summary'):
        return []
    if role == 'assistant' and (row.get('tool_calls') or row.get('has_tool_calls')):
        calls = row.get('tool_calls') if isinstance(row.get('tool_calls'), list) else []
        return [{'row_id': row['id'], 'role': 'tool', 'name': display_text(call['function']['name'], 100),
                 'display_id': row['display_id'],
                 'tool_call_id': str(call.get('id') or '')[:200], 'text': '', 'status': 'started', 'truncated': False}
                for call in calls[:16] if isinstance(call, dict)
                and isinstance(call.get('function'), dict) and isinstance(call['function'].get('name'), str)
                and call['function']['name'] not in ('_thinking', 'thinking', 'reasoning')]
    text = display_text(row.get('content'))
    if not text and role == 'assistant':
        return []
    name = row.get('tool_name') or 'tool'
    if role == 'tool' and name in ('_thinking', 'thinking', 'reasoning'):
        return []
    result = {'row_id': row['id'], 'display_id': row['display_id'], 'role': role, 'text': text, 'truncated': bool(row.get('oversized')) or len(text) == 12000}
    if role == 'tool':
        result['name'] = display_text(name, 100)
        result['status'] = 'returned'
        if isinstance(row.get('tool_call_id'), str):
            result['tool_call_id'] = row['tool_call_id'][:200]
    if isinstance(row.get('timestamp'), (int, float)):
        result['timestamp'] = row['timestamp']
    return [result]


def _history_records(store, sid, before, limit):
    """Page fixed-width native display identities in SQLite; fetch at most 100 payloads."""
    from hermes_state_messages import DISPLAY_VISIBLE_SQL
    lineage = store.db.get_compression_lineage(sid) or [sid]
    marks = ','.join('?' for _ in lineage)
    with store.db._read_ctx() as conn:
        # Modern writers persist this identity. A legacy unindexed row keeps its own cursor;
        # this read never backfills indexes or loads transcript bodies to derive one.
        selected = conn.execute(f'''WITH candidates AS (
            SELECT id,active,CASE WHEN display_identity IS NULL THEN 'row:'||id ELSE hex(display_identity) END AS identity
            FROM messages WHERE session_id IN ({marks}) AND (active=1 OR compacted=1){DISPLAY_VISIBLE_SQL}
        ), page AS (
            SELECT identity,MIN(id) AS ordinal FROM candidates GROUP BY identity
            HAVING MIN(id)<? ORDER BY ordinal DESC LIMIT ?
        ) SELECT ordinal,identity,(SELECT id FROM candidates c WHERE c.identity=page.identity
                          ORDER BY active DESC,id DESC LIMIT 1) AS representative FROM page ORDER BY ordinal DESC''',
                                (*lineage, before if before is not None else 2**63 - 1, limit + 1)).fetchall()
        ids = [r['representative'] for r in selected[:limit]]
        if not ids:
            return [], False
        placeholders = ','.join('?' for _ in ids)
        rows = {row['id']: row for row in conn.execute(f'SELECT {_VISIBLE_COLUMNS} FROM messages WHERE id IN ({placeholders})', ids)}
    return [(r['ordinal'], r['identity'], rows[r['representative']]) for r in selected[:limit]], len(selected) > limit


def _history(connection, params):
    before, limit = _before(params.get('before')), _limit(params.get('limit'), 40)
    with _store(connection) as store:
        project = _owned(store, params.get('subagent_id'))
        sid = store.resolve(project['session_id'])
        rows, has_more = _history_records(store, sid, before, limit)
        messages, consumed = [], []
        for order, identity, raw in rows:
            row = store.db._row_to_message_dict(raw, warn_context='subagent display', summary_flag=True)
            row['id'], row['display_id'] = order, identity
            projected = _history_row(row)
            if len(json.dumps(messages + projected).encode()) > _BUDGET:
                break
            consumed.append(order)
            if projected:
                messages.extend(reversed(projected))
        return {'subagent_id': project['subagent_id'], 'messages': list(reversed(messages)),
                'page': {'before': consumed[-1] if consumed else 0, 'has_more': has_more or len(rows) > len(consumed)}}


async def rpc(connection, ref, params, *, history=False):
    allowed = {'subagent_id', 'before', 'limit'} if history else {'before', 'limit', 'tracked'}
    if set(params) - allowed:
        raise RuntimeStoreError('invalid_params')
    return await asyncio.to_thread(_history if history else _list, connection, params)


def changed(turn, event_type):
    """A display invalidation survives parent-turn completion; it never admits or delivers work."""
    try:
        _changed(turn, event_type)
    except Exception:  # health: allow BLE001 -- display observers must never fail task execution
        logger.debug('Subagent display invalidation unavailable', exc_info=True)


def _changed(turn, event_type):
    if event_type not in ('subagent.start', 'subagent.tool', 'subagent.progress', 'subagent.complete', 'subagent.changed'):
        return
    owner = getattr(turn, '_approval_owner', None)
    if owner is None:
        return
    authority, sid, _generation = owner
    from hermes_runtime.adapters.hermes import enabled, managed_home
    from hermes_runtime.sessions import HOME_SESSION_ID
    from pathlib import Path
    if (not enabled() or sid != HOME_SESSION_ID or Path(authority.profile_id).resolve() != managed_home()
            or getattr(authority, 'retired', False)):
        return
    live = authority.sessions.get(sid)
    if live is not None:
        live.event_stream.publish(sid, {}, event_type='panel.subagents.changed')
