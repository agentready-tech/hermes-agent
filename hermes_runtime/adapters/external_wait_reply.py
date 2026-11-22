"""Read an original retained native messaging admission, not a transcript copy."""

import json
import re
import sqlite3


def proves_reply(conn, home, current_session, wait, ref):
    from hermes_runtime.adapters.state import find_home_tip
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_state_runtime import RuntimeStoreError, _canonical_chain
    from hermes_state_terminal import identity_key

    if not isinstance(ref, dict) or set(ref) != {'kind', 'admission_id'} or ref['kind'] != 'session_admission':
        return False
    aid = ref['admission_id']
    if not isinstance(aid, str) or re.fullmatch(r'[0-9a-f]{32}', aid) is None:
        return False
    watermarks = wait.get('input_watermarks')
    floor = watermarks.get('session_admissions') if isinstance(watermarks, dict) else None
    if wait.get('mode') != 'resume_main' or type(floor) is not int or floor < 0:
        return False
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    row = cursor.execute('SELECT * FROM session_admissions WHERE admission_id=?', (aid,)).fetchone()
    if row is None or row['seq'] <= floor or row['intent'] != 'queue':
        return False
    if (row['status'], row['outcome']) != ('terminal', 'completed'):
        return False
    try:
        chain = _canonical_chain(conn, HOME_SESSION_ID)
        lineage = json.loads(row['lineage_json'])
        payload = json.loads(row['payload_json'])
    except (ValueError, RuntimeStoreError):
        return False
    target = row['target_session_id']
    if current_session != chain[-1] or current_session != find_home_tip(conn) or not _matches_lineage(lineage, target, chain):
        return False
    if not _original_native_message(row, payload, home):
        return False
    principal, request = row['principal_id'], row['request_id']
    for session in chain:
        if conn.execute('SELECT 1 FROM session_admissions WHERE principal_id=? AND target_session_id=? '
                        'AND request_id=? AND seq<?', (principal, session, request, row['seq'])).fetchone():
            return False
        if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (identity_key(principal, session, request),)).fetchone():
            return False
    return True


def _matches_lineage(lineage, target, chain):
    if (target not in chain or not isinstance(lineage, list) or not lineage
            or lineage[0] != target):
        return False
    start = chain.index(target)
    return lineage == chain[start:start + len(lineage)]


def _original_native_message(row, payload, home):
    env = payload.get('native_text_v1') if isinstance(payload, dict) else None
    if not isinstance(env, dict) or 'automation' in env:
        return False
    provenance, event, source = (env.get(k) for k in ('provenance', 'event', 'source'))
    if not all(isinstance(item, dict) for item in (provenance, event, source)):
        return False
    if (provenance.get('runtime_home') != str(home) or provenance.get('platform') != source.get('platform')
            or not provenance.get('connector') or source.get('is_bot') is not False):
        return False
    identity = [source.get(k) for k in ('profile', 'platform', 'chat_id', 'thread_id', 'user_id')]
    principal = 'messaging:' + json.dumps(identity, separators=(',', ':'))
    return (row['principal_id'] == principal and bool(event.get('message_id'))
            and str(event['message_id']) == row['request_id']
            and isinstance(payload.get('text'), str) and bool(payload['text'].strip()))
