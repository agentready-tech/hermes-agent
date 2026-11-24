"""Project canonical delegation mail without exposing its model-only envelope."""
import hashlib
import json

from hermes_runtime.adapters.main_assignments import _ASSIGNMENT_SCOPE, _cursor, _lineage, _nodes
from hermes_runtime.adapters.main_subagents import display_text
from hermes_state_common import _sql_json_extract
from hermes_state_messages import DISPLAY_VISIBLE_SQL
from tools.delegate_mail_store import MAIL_INSTRUCTIONS, mail_header


def _delivered_body(store, message, history):
    """An acknowledged message's SQL body is empty; its transcript owns the exact text."""
    delivery_id = message['delivery_id']
    if not delivery_id:
        return ''
    marks = ','.join('?' for _ in history)
    stable_id = _sql_json_extract('display_metadata', '$.agent_mail_delivery_id')
    row = store.db._read_one(
        f"""SELECT content,display_metadata FROM messages WHERE session_id IN ({marks})
            AND (platform_message_id=? OR {stable_id}=?) AND role='user' AND display_kind='agent_message'
            AND (active=1 OR compacted=1) AND NOT _compressed_summary
            AND typeof(content)='text' AND length(content)<=64000 {DISPLAY_VISIBLE_SQL}
            AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(display_metadata)
                 THEN display_metadata ELSE '{{}}' END,'$.agent_message_ids') WHERE value=?)
            ORDER BY active DESC,id DESC LIMIT 1""",
        (*history, delivery_id, delivery_id, message['message_id']),
    )
    if row is None:
        return ''
    content = store.db._decode_content(row['content'])
    if not isinstance(content, str):
        return ''
    ids = json.loads(row['display_metadata'])['agent_message_ids']
    if not isinstance(ids, list):
        return ''
    index = ids.index(message['message_id'])
    prefix = mail_header(message['message_id'], message['sender_session_id'], message['kind'])
    if index:
        prefix = '\n\n' + prefix
    elif not content.startswith(prefix):
        return ''
    suffix = MAIL_INSTRUCTIONS
    if index + 1 < len(ids):
        next_row = store.db._read_one(
            'SELECT sender_session_id,kind FROM delegation_mail WHERE message_id=? AND delivery_id=?',
            (ids[index + 1], delivery_id),
        )
        if next_row is None:
            return ''
        suffix = '\n\n' + mail_header(ids[index + 1], next_row['sender_session_id'], next_row['kind'])
    return _checked_body(content, prefix, suffix, message['content_hash'])


def _checked_body(content, prefix, suffix, digest):
    # A body can itself contain a string that looks like an envelope marker.
    # Only the candidate matching this receipt's hash is its actual content.
    start = content.find(prefix)
    while start != -1:
        body_start = start + len(prefix)
        end = content.find(suffix, body_start)
        while end != -1 and end - body_start <= 16000:
            body = content[body_start:end]
            if hashlib.sha256(body.encode()).hexdigest() == digest:
                return body
            end = content.find(suffix, end + len(suffix))
        start = content.find(prefix, body_start)
    return ''


def _session_link(store, history):
    marks = ','.join('?' for _ in history)
    projects = store.db._read_all(
        f'SELECT subagent_id,parent_session_id FROM delegated_projects WHERE session_id IN ({marks}) LIMIT 2',
        tuple(history),
    )
    if projects:
        return tuple(projects[0]) if len(projects) == 1 else (None, None)
    session = store.db.get_session(history[0])
    return (session['id'], session['parent_session_id']) if session else (None, None)


def _offpage_peer(store, sid, root, project_id):
    """Resolve an endpoint through the same two-level owned graph used by Panel."""
    history = _lineage(store, sid)
    identity, parent = _session_link(store, history)
    if parent is None:
        return None
    if parent not in root:
        parent_history = _lineage(store, parent)
        parent_id, grandparent = _session_link(store, parent_history)
        if grandparent not in root:
            return None
        parents, _ = _nodes(store, store.parent_id, project_id, _cursor(None), 1, identity=parent_id)
        if not parents or parents[0]['session_id'] not in parent_history:
            return None
        parent = parents[0]['session_id']
    peers, _ = _nodes(store, parent, project_id, _cursor(None), 1, identity=identity)
    if peers and peers[0]['session_id'] in history:
        return identity, display_text(peers[0]['title'], 120), history
    return None


def _mail_rows(store, visible, root, project_id, before, limit, focus, counterpart):
    slots = ','.join('(?,?)' for _ in visible)
    marks = ','.join('?' for _ in root)
    discover = focus is not None and counterpart is None
    return store.db._read_all(
        f"""WITH {_ASSIGNMENT_SCOPE}, roster(sid,node_id) AS (VALUES {slots})
            SELECT m.message_id,m.kind,m.sender_session_id,m.target_session_id,m.delivery_id,
                   m.content_hash,m.content,m.accepted_at
            FROM delegation_mail m LEFT JOIN roster sender ON sender.sid=m.sender_session_id
              LEFT JOIN roster target ON target.sid=m.target_session_id
            WHERE m.root_session_id IN ({marks}) AND m.kind='message'
              AND ((SELECT goal_id FROM assignment_root) IS NULL
                   OR json_extract(m.origin_json,'$.message_goal_project_id') IN assignment_goals)
              AND (? OR (sender.node_id IS NOT NULL AND target.node_id IS NOT NULL))
              AND (? IS NULL OR ? IN (sender.node_id,target.node_id))
              AND (? IS NULL OR ? IN (sender.node_id,target.node_id))
              AND (m.accepted_at,'mail:'||m.message_id)<(?,?)
            ORDER BY m.accepted_at DESC,m.message_id DESC LIMIT ?""",
        (project_id, *(field for pair in visible.items() for field in pair), *root, discover,
         focus, focus, counterpart, counterpart, *before, limit + 1),
    )


def mail_events(store, nodes, project_id, before, limit, focus, counterpart):
    root = _lineage(store, store.parent_id)
    lineages = {'main': root}
    lineages.update((node['id'], _lineage(store, node['session_id'])) for node, _parent, _scope in nodes)
    visible = {sid: identity for identity, history in lineages.items() for sid in history}
    resolved = {sid: (identity, None, lineages[identity]) for sid, identity in visible.items()}
    events = []

    def resolve(sid):
        if sid not in resolved:
            resolved[sid] = _offpage_peer(store, sid, root, project_id)
        return resolved[sid]

    while len(events) <= limit:
        rows = _mail_rows(store, visible, root, project_id, before, limit, focus, counterpart)
        for row in rows:
            sender, target = resolve(row['sender_session_id']), resolve(row['target_session_id'])
            if sender is None or target is None:
                continue
            body = row['content'] or _delivered_body(store, row, target[2])
            event = {'id': 'mail:' + row['message_id'], 'time': row['accepted_at'], 'kind': 'message',
                     'from': sender[0], 'to': target[0], 'text': display_text(body, 2400)}
            if sender[1] is not None:
                event['from_name'] = sender[1]
            if target[1] is not None:
                event['to_name'] = target[1]
            events.append(event)
            if len(events) > limit:
                break
        if len(rows) <= limit:
            break
        before = (rows[-1]['accepted_at'], 'mail:' + rows[-1]['message_id'])
    return events
