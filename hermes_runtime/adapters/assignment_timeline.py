"""Chronological, paged display of verified delegation receipts and visible tool history."""

import json

from hermes_runtime.adapters.main_assignments import (
    _ASSIGNMENT_SCOPE, _children, _cursor, _goal_row, _lineage, _node_id, _page, _root_node, one_shot_status,
)
from hermes_runtime.adapters.main_subagents import _BUDGET, _VISIBLE_COLUMNS, _history_row, _limit, display_text
from hermes_state_messages import DISPLAY_VISIBLE_SQL
from hermes_state_runtime import RuntimeStoreError

_VISIBLE = (" AND (active=1 OR compacted=1) AND NOT _compressed_summary"
            " AND COALESCE(display_kind,'') NOT IN ('hidden','agent_message','internal_notification')"
            + DISPLAY_VISIBLE_SQL)


def _plain(store, history, *, role, receipt=None):
    marks = ','.join('?' for _ in history)
    row = store.db._read_one(
        f"""SELECT CASE WHEN length(content)<64000 THEN content END AS content,timestamp
            FROM messages WHERE session_id IN ({marks}) AND role=? {_VISIBLE}
            AND (? IS NULL OR platform_message_id=?)
            AND (role!='assistant' OR ((tool_calls IS NULL OR tool_calls='[]') AND finish_reason IN ('stop','end_turn')))
            ORDER BY id {'ASC' if role == 'user' else 'DESC'} LIMIT 1""",
        (*history, role, receipt, receipt),
    )
    if not row:
        return '', None
    return display_text(store.db._decode_content(row['content']), 2400), row['timestamp']


def _receipt_events(store, node, parent, project_id, history, before, limit):
    rows = store.db._read_all(
        f"""WITH {_ASSIGNMENT_SCOPE}, scoped AS (
            SELECT message_id,accepted_at,completed_at,content,state,
                   CASE WHEN length(result_json)<=64000 THEN result_json END AS result_json
            FROM delegated_project_messages WHERE subagent_id=?
            AND ((SELECT goal_id FROM assignment_root) IS NULL OR json_extract(origin_json, '$.goal_project_id') IN assignment_goals)
            AND COALESCE(json_extract(options_json, '$.agent_mail_wake'), 0)=0
            AND COALESCE(event_key,'') NOT GLOB 'mail-wake:*'
            AND COALESCE(event_key,'') NOT GLOB 'goal:project-*:mail-wake:*'
        ), receipts AS (
            SELECT 'dispatch:'||message_id AS id,accepted_at AS time,message_id,'task' AS kind,
                   CASE WHEN length(content)<=64000 THEN content END AS content,state FROM scoped
            UNION ALL
            SELECT 'return:'||message_id,completed_at,message_id,'result',result_json,state
            FROM scoped WHERE completed_at IS NOT NULL
        ) SELECT * FROM receipts WHERE (time,id)<(?,?) ORDER BY time DESC,id DESC LIMIT ?""",
        (project_id, node['id'], *before, limit + 1),
    )
    events = []
    for row in rows:
        is_task = row['kind'] == 'task'
        if is_task:
            body = display_text(row['content'], 2400)
            if not body:
                body, _ = _plain(store, history, role='user', receipt=row['message_id'])
        else:
            result = json.loads(row['content'] or '{}')
            body = display_text(result.get('summary') or result.get('error'), 2400) if isinstance(result, dict) else ''
        events.append({'id': row['id'], 'time': row['time'], 'kind': row['kind'],
                       'from': parent if is_task else node['id'], 'to': node['id'] if is_task else parent,
                       'status': row['state'], 'text': body})
    return events


def _one_shot(store, node, parent, history):
    body, _ = _plain(store, history, role='user')
    identity = node['id']
    events = [{'id': 'dispatch:' + identity, 'time': node['time'], 'kind': 'task',
               'from': parent, 'to': identity, 'text': body}]
    result, timestamp = _plain(store, history, role='assistant')
    status = one_shot_status(store, node)
    if result and timestamp is not None:
        events.append({'id': 'return:' + identity, 'time': timestamp, 'kind': 'result',
                       'from': identity, 'to': parent, 'text': result, 'status': status})
    return events


def _tools(store, node, project_id, history, before, limit):
    marks = ','.join('?' for _ in history)
    ranges = ''
    parameters = tuple(history)
    if project_id:
        # Canonical dispatch IDs were saved on the worker's user-input row. Tie
        # each action to that physical turn, even when a worker serves two Goals.
        ranges = f"""{_ASSIGNMENT_SCOPE}, turns AS (
            SELECT session_id,id,platform_message_id,
                   LEAD(id) OVER (PARTITION BY session_id ORDER BY id) AS next_id
            FROM messages WHERE session_id IN ({marks}) AND role='user' AND (active=1 OR compacted=1)
            AND (platform_message_id GLOB 'project-msg-*'
                 OR COALESCE(display_kind,'') NOT IN ('agent_message','auto_continue'))
        ),"""
        scope = """ AND EXISTS (
            SELECT 1 FROM turns t JOIN delegated_project_messages p ON p.message_id=t.platform_message_id
            WHERE t.session_id=messages.session_id AND messages.id>t.id
              AND (t.next_id IS NULL OR messages.id<t.next_id)
              AND p.subagent_id=? AND json_extract(p.origin_json, '$.goal_project_id') IN assignment_goals
        )"""
        parameters = (project_id, *history, *history, node['id'])
    else:
        scope = ''
    rows = store.db._read_all(
        f"""WITH {ranges} candidates AS (
            SELECT id,active,timestamp,
                   CASE WHEN display_identity IS NULL THEN 'row:'||id ELSE hex(display_identity) END AS identity
            FROM messages WHERE session_id IN ({marks}) {_VISIBLE}
            AND (role='tool' OR (role='assistant' AND tool_calls IS NOT NULL AND tool_calls!='[]')) {scope}
        ), cuts AS (
            SELECT MIN(id) AS ordinal,identity,MIN(timestamp) AS time FROM candidates GROUP BY identity
        ), page AS (
            SELECT *, 'tool:'||printf('%020d',ordinal) AS event_id FROM cuts
            WHERE (time,'tool:'||printf('%020d',ordinal))<=(?,?)
            ORDER BY time DESC,ordinal DESC LIMIT ?
        ) SELECT page.*, {_VISIBLE_COLUMNS}
          FROM page JOIN messages ON messages.id=(
            SELECT c.id FROM candidates c WHERE c.identity=page.identity ORDER BY c.active DESC,c.id DESC LIMIT 1
          ) ORDER BY page.time DESC,page.ordinal DESC""",
        (*parameters, *before, limit + 1),
    )
    events = []
    for row in rows:
        native = store.db._row_to_message_dict(row, warn_context='assignment actions', summary_flag=True)
        native['display_id'] = row['identity']
        for index, action in enumerate(_history_row(native)):
            if not action.get('name'):
                continue
            events.append({'id': f"{row['event_id']}:{index}", 'time': row['time'], 'kind': 'tool',
                           'from': node['id'], 'to': node['id'], 'status': action['status'],
                           'tool': action['name'], 'text': action['text'][:2400]})
    return events


def _node_events(store, node, parent, project_id, before, limit, tools):
    history = _lineage(store, node['session_id'])
    if node['kind'] == 'project':
        events = _receipt_events(store, node, parent, project_id, history, before, limit)
    else:
        events = _one_shot(store, node, parent, history)
    return events + _tools(store, node, project_id, history, before, limit) if tools else events


def _timeline_nodes(store, keys, project_id):
    if not isinstance(keys, list) or len(keys) > 100:
        raise RuntimeStoreError('invalid_params')
    roots, seen = {}, set()
    for key in keys:
        if not isinstance(key, dict) or set(key) != {'id', 'parent_id'}:
            raise RuntimeStoreError('invalid_params')
        identity, parent_id = _node_id(key['id']), _node_id(key['parent_id'])
        if identity in seen:
            raise RuntimeStoreError('invalid_params')
        seen.add(identity)
        if parent_id == 'main':
            root = _root_node(store, identity, project_id)
            roots[identity] = root
            yield root, parent_id, project_id
            continue
        if parent_id not in roots:
            roots[parent_id] = _root_node(store, parent_id, project_id)
        children, _ = _children(store, roots[parent_id], project_id, _cursor(None), 1, identity=identity)
        if not children:
            raise RuntimeStoreError('not_found')
        yield children[0], parent_id, project_id


def _focus(nodes, selection):
    if selection is None:
        return None, None
    if (not isinstance(selection, dict) or set(selection) != {'type', 'id'}
            or selection['type'] not in ('node', 'edge')):
        raise RuntimeStoreError('invalid_params')
    focus = _node_id(selection['id'])
    if selection['type'] == 'node' and focus == 'main':
        return focus, None
    parents = {node['id']: parent for node, parent, _ in nodes}
    if focus not in parents:
        raise RuntimeStoreError('not_found')
    return focus, parents[focus] if selection['type'] == 'edge' else None


def timeline(store, params):
    from hermes_runtime.adapters.assignment_mail import mail_events

    before, limit = _cursor(params.get('before')), min(_limit(params.get('limit'), 40), 50)
    project_id = params.get('project_id')
    row = _goal_row(store, project_id) if project_id is not None else None
    tools = params.get('tools', False)
    if type(tools) is not bool:
        raise RuntimeStoreError('invalid_params')
    nodes = list(_timeline_nodes(store, params.get('node_ids', []), project_id))
    focus, counterpart = _focus(nodes, params.get('selection'))
    events = mail_events(store, nodes, project_id, before, limit, focus, counterpart)
    if row is not None and focus in (None, 'main'):
        events.append({'id': 'open:' + project_id, 'time': row['created_at'], 'kind': 'goal',
                       'from': 'main', 'to': 'main', 'text': display_text(row['goal'], 2400)})
    for node, parent, scope in nodes:
        if not focus or node['id'] == focus or (not counterpart and parent == focus):
            show_tools = tools and not counterpart and (not focus or node['id'] == focus)
            events.extend(_node_events(store, node, parent, scope, before, limit, show_tools))
    events = sorted((event for event in events if (event['time'], event['id']) < before),
                    key=lambda event: (event['time'], event['id']), reverse=True)
    page, size = [], 0
    for event in events[:limit]:
        size += len(json.dumps(event, ensure_ascii=False).encode())
        if size > _BUDGET:
            break
        page.append(event)
    return {'events': list(reversed(page)), 'page': _page(page, len(events) > len(page))}
