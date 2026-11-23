"""Managed Panel projection and control of the owning main's Project Goals."""

import asyncio
from datetime import UTC, datetime
from functools import partial
import json
import re

from hermes_runtime.adapters.main_subagents import _limit, _store, display_text, publish_change
from hermes_state_runtime import RuntimeStoreError
from tools.delegate_project_goal import close_goal, set_goal_paused

_PREFIX = 'panel.subagents.'
_ID = re.compile(r'[A-Za-z0-9_:-]{1,160}')
_GOAL_ID = re.compile(r'project-[a-f0-9]{32}')
_CLOSED = frozenset({'done', 'cancelled', 'inactive'})
_CHILD_PAGE_SIZE = 6
_FIELDS = {
    'assignments': {'before', 'limit'},
    'graph': {'project_id', 'parent_id', 'before', 'limit'},
    'timeline': {'project_id', 'node_ids', 'before', 'limit', 'tools', 'selection'},
    'control': {'project_id', 'action'},
}


def _node_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RuntimeStoreError('invalid_params')
    return value


def _project_id(value):
    if not isinstance(value, str) or not _GOAL_ID.fullmatch(value):
        raise RuntimeStoreError('invalid_params')
    return value


def _lineage(store, sid):
    return store.db.get_compression_lineage(sid) or [sid]


def _goal_row(store, project_id):
    _project_id(project_id)
    row = store.db._read_one('SELECT * FROM delegated_projects WHERE subagent_id=?', (project_id,))
    try:
        row = store._owned(row)
    except ValueError as exc:
        raise RuntimeStoreError('not_found') from exc
    if not isinstance(json.loads(row['workspace_json']).get('goal_state'), dict):
        raise RuntimeStoreError('not_found')
    return row


def _next_run(project_id, wait):
    from cron.wait_evidence import live_timer

    job = live_timer(wait, project_id, datetime.now(UTC)) if isinstance(wait, dict) else None
    return job.get('next_run_at') if job else None


def _goal_card(row, *, detail=False):
    project_id = row['subagent_id']
    workspace = json.loads(row['workspace_json'])
    goal = workspace['goal_state']
    status = goal['status']
    if status not in _CLOSED and status != 'closing':
        status = 'inactive' if row['archived'] else 'paused' if row['paused'] else status
    card = {'project_id': project_id, 'title': display_text(row['goal'], 300),
            'status': status, 'stage': display_text(goal.get('stage'), 300),
            'created_at': row['created_at']}
    if detail:
        contract = goal['contract']
        source = goal.get('source') or {}
        card.update(description=display_text(contract.get('outcome'), 3000),
                    verification=display_text(contract.get('verification'), 2000),
                    constraints=display_text(contract.get('constraints'), 2000),
                    next_step=display_text(goal.get('next_step'), 1500),
                    blocker=display_text(goal.get('blocker'), 1500),
                    unblock=display_text(goal.get('unblock'), 1500),
                    source=display_text(source.get('id'), 300) if source.get('kind') == 'tracker' else None,
                    next_check_at=_next_run(project_id, workspace.get('external_wait')) if status == 'waiting' else None)
    return card


def _assignments(store, params):
    before = params.get('before', '1:9223372036854775807')
    limit = min(_limit(params.get('limit'), 20), 40)
    if not isinstance(before, str) or not re.fullmatch(r'[01]:[0-9]{1,19}', before):
        raise RuntimeStoreError('invalid_params')
    tier, ordinal = (int(value) for value in before.split(':'))
    if ordinal > 2**63 - 1:
        raise RuntimeStoreError('invalid_params')
    parents = _lineage(store, store.parent_id)
    marks = ','.join('?' for _ in parents)
    rows = store.db._read_all(
        f"""WITH goals AS (
          SELECT rowid AS ordinal, subagent_id, goal, workspace_json, archived, paused, created_at,
            NOT (archived=1 OR json_extract(workspace_json, '$.goal_state.status') IN ('done','cancelled','inactive')) AS priority
          FROM delegated_projects WHERE parent_session_id IN ({marks})
            AND json_type(workspace_json, '$.goal_state')='object'
        ) SELECT * FROM goals
          WHERE priority<? OR (priority=? AND ordinal<?)
          ORDER BY priority DESC,ordinal DESC LIMIT ?""",
        (*parents, tier, tier, ordinal, limit + 1),
    )
    cards = [_goal_card(row) for row in rows[:limit]]
    cursor = f"{rows[len(cards)-1]['priority']}:{rows[len(cards)-1]['ordinal']}" if cards else None
    return {'goals': cards, 'page': {'before': cursor, 'has_more': len(rows) > limit}}


def _cursor(value):
    if value is None:
        return (1e20, '')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,14}(?:\.[0-9]{1,20})?\|[A-Za-z0-9_:-]{1,220}', value):
        raise RuntimeStoreError('invalid_params')
    ordinal, identity = value.split('|', 1)
    return float(ordinal), identity


def _page(items, more):
    last = items[-1] if items else None
    return {'before': f"{last['time']:.17f}|{last['id']}" if last else None, 'has_more': more}


_GOAL_LINK = """(? IS NULL OR json_extract(d.workspace_json, '$.goal_owner_id')=? OR EXISTS (
    SELECT 1 FROM delegated_project_messages m WHERE m.subagent_id=d.subagent_id
      AND json_extract(m.origin_json, '$.goal_project_id')=?))"""


def _nodes(store, parent_sid, project_scope, before, limit, *, identity=None):
    """An exact delegate creation or persistent worker; other session forks stay out."""
    from hermes_state_common import _sql_json_extract

    parents = _lineage(store, parent_sid)
    marks = ','.join('?' for _ in parents)
    delegator = _sql_json_extract('s.model_config', '$._delegate_from')
    branch = _sql_json_extract('s.model_config', '$._branched_from')
    reset = _sql_json_extract('s.model_config', '$._reset_from')
    rows = store.db._read_all(
        f"""WITH delegates AS (
          SELECT subagent_id AS id, session_id, project_key AS title, d.goal, created_at AS time,
            'project' AS kind, archived, paused, NULL AS ended_at
          FROM delegated_projects d WHERE parent_session_id IN ({marks})
            AND json_type(d.workspace_json, '$.goal_state') IS NULL AND {_GOAL_LINK}
          UNION ALL
          SELECT s.id,s.id,s.title,'',s.started_at,'session',0,0,s.ended_at
          FROM sessions s WHERE ? IS NULL AND s.parent_session_id IN ({marks})
            AND {branch} IS NULL AND {reset} IS NULL
            AND ({delegator}=s.parent_session_id OR ({delegator} IS NULL AND s.source IN ('delegate','subagent') AND NOT EXISTS (
              SELECT 1 FROM sessions previous WHERE previous.id=s.parent_session_id AND previous.end_reason='compression'
            )))
            AND NOT EXISTS (SELECT 1 FROM delegated_projects d WHERE d.subagent_id=s.id OR d.session_id=s.id)
        ) SELECT * FROM delegates WHERE (? IS NULL OR id=?)
          AND (time,id)<(?,?) ORDER BY time DESC,id DESC LIMIT ?""",
        (*parents, project_scope, project_scope, project_scope, project_scope, *parents,
         identity, identity, *before, limit + 1),
    )
    return [dict(row) for row in rows[:limit]], len(rows) > limit


def _children(store, parent, project_id, before, limit, *, identity=None):
    return _nodes(store, parent['session_id'], project_id, before, limit, identity=identity)


def _persistent_status(store, node, project_id):
    row = store.db._read_one(
        """SELECT state FROM delegated_project_messages WHERE subagent_id=?
           AND (? IS NULL OR json_extract(origin_json, '$.goal_project_id')=?)
           ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, rowid DESC LIMIT 1""",
        (node['id'], project_id, project_id),
    )
    return row['state'] if row else 'idle'


def one_shot_status(store, node):
    from hermes_state_messages import DISPLAY_VISIBLE_SQL

    sid = store.resolve(node['session_id'])
    current = store.db.get_session(sid) or node
    status = store.db.get_session_model_config_value(sid, '_delegate_status')
    if status in ('completed', 'failed', 'error', 'timeout', 'interrupted', 'cancelled'):
        return status
    if not current.get('ended_at'):
        return 'running'
    reason = current.get('end_reason')
    if reason in ('completed', 'failed', 'error', 'timeout', 'interrupted', 'cancelled'):
        return reason
    # A legacy agent_close is only a resource event. A final user-visible
    # answer or a stamped failure is required to claim an execution outcome.
    final = store.db._read_one(
        f"""SELECT finish_reason FROM messages WHERE session_id=? AND role='assistant'
            AND (active=1 OR compacted=1) AND NOT _compressed_summary
            AND COALESCE(display_kind,'') NOT IN ('hidden','internal_notification','agent_message')
            AND (tool_calls IS NULL OR tool_calls='[]'){DISPLAY_VISIBLE_SQL} ORDER BY id DESC LIMIT 1""",
        (sid,),
    )
    return {'stop': 'completed', 'end_turn': 'completed', 'error': 'failed', 'interrupted': 'interrupted'}.get(
        final['finish_reason'] if final else None, 'unknown')


def _node_card(store, node, parent_id, project_id=None):
    if node['archived']:
        status = 'archived'
    elif node['paused']:
        status = 'paused'
    elif node['kind'] == 'project':
        status = _persistent_status(store, node, project_id)
    else:
        status = one_shot_status(store, node)
    return {'id': node['id'], 'parent_id': parent_id, 'time': node['time'], 'kind': node['kind'],
            'name': display_text(node['title'], 120), 'goal': display_text(node['goal'], 200), 'status': status}


def _root_node(store, identity, project_id):
    nodes, _ = _nodes(store, store.parent_id, project_id, _cursor(None), 1, identity=_node_id(identity))
    if not nodes:
        raise RuntimeStoreError('not_found')
    return nodes[0]


def _graph(store, params):
    project_id = params.get('project_id')
    detail = _goal_card(_goal_row(store, project_id), detail=True) if project_id is not None else None
    parent_id = params.get('parent_id')
    default_limit = _CHILD_PAGE_SIZE if parent_id is not None else 12
    before, limit = _cursor(params.get('before')), min(_limit(params.get('limit'), default_limit), 12)
    if parent_id is not None:
        parent = _root_node(store, parent_id, project_id)
        children, more = _children(store, parent, project_id, before, limit)
        return {'goal': detail, 'nodes': [_node_card(store, node, parent_id, project_id) for node in children],
                'page': _page(children, more)}
    first, more = _nodes(store, store.parent_id, project_id, before, limit)
    nodes = []
    for parent in first:
        root = _node_card(store, parent, 'main', project_id)
        children, more_children = _children(store, parent, project_id, _cursor(None), _CHILD_PAGE_SIZE)
        root['child_page'] = _page(children, more_children)
        nodes.append(root)
        nodes.extend(_node_card(store, child, parent['id'], project_id) for child in children)
    return {'goal': detail, 'nodes': nodes, 'page': _page(first, more)}


def _control(connection, store, params):
    from hermes_runtime.adapters.goal_turn import resume_paused_goal
    from hermes_runtime.sessions import HOME_SESSION_ID

    project_id, action = params.get('project_id'), params.get('action')
    row = _goal_row(store, project_id)
    if action not in ('pause', 'resume', 'cancel'):
        raise RuntimeStoreError('invalid_params')
    if row['archived']:
        raise RuntimeStoreError('invalid_transition')
    try:
        if action == 'cancel':
            close_goal(store, project_id, 'cancelled')
        elif action == 'pause':
            set_goal_paused(store, project_id, True)
        else:
            resume_paused_goal(store, project_id)
    except ValueError as exc:
        raise RuntimeStoreError('invalid_transition') from exc
    publish_change(connection.authority, HOME_SESSION_ID)
    return {'goal': _goal_card(_goal_row(store, project_id), detail=True)}


def _run(connection, method, params):
    if params.keys() - _FIELDS[method]:
        raise RuntimeStoreError('invalid_params')
    with _store(connection, 'session:control' if method == 'control' else 'session:read') as store:
        if method == 'control':
            return _control(connection, store, params)
        if method == 'assignments':
            return _assignments(store, params)
        if method == 'graph':
            return _graph(store, params)
        from hermes_runtime.adapters.assignment_timeline import timeline

        return timeline(store, params)


async def _rpc(connection, method, _ref, params):
    return await asyncio.to_thread(_run, connection, method, params)


def handlers(connection):
    return {_PREFIX + method: partial(_rpc, connection, method) for method in _FIELDS}
