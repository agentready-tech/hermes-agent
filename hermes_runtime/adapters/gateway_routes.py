"""Repair the old collision between canonical local receipts and the managed home route."""
import json
import logging
import time
import uuid
from datetime import datetime

from hermes_runtime.sessions import HOME_SESSION_KEY, HOME_SESSION_TITLE


def _move_local_index(conn, lineage, route):
    for row in conn.execute("SELECT scope,entry_json FROM gateway_routing WHERE session_key=?",
                            (HOME_SESSION_KEY,)).fetchall():
        entry = json.loads(row["entry_json"])
        if entry["session_id"] not in lineage:
            continue
        entry["session_key"] = route
        conn.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES(?,?,?,?)",
                     (row["scope"], route, json.dumps(entry), time.time()))
        conn.execute("DELETE FROM gateway_routing WHERE scope=? AND session_key=?",
                     (row["scope"], HOME_SESSION_KEY))


def _move_unstarted_messages(conn, lineage, home_id):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_state_keys import admission_fingerprint

    source = None
    for sid in lineage:
        rows = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? "
                            "AND status='queued' AND generation IS NULL ORDER BY seq", (sid,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            envelope = payload.get("native_text_v1")
            if not isinstance(envelope, dict) or envelope.get("route") != HOME_SESSION_KEY:
                continue
            candidate = SessionSource.from_dict(envelope["source"])
            if candidate.platform == Platform.LOCAL:
                continue
            digest = admission_fingerprint(canonical_target=home_id,
                payload={"input": payload, "intent": row["intent"]})
            # Keep the old receipt for audit. Only never-started work gets a new target,
            # with the native idempotency key and a fresh digest for that target.
            previous = conn.execute("SELECT payload_digest FROM session_admissions WHERE principal_id=? "
                "AND target_session_id=? AND request_id=?", (row["principal_id"], home_id, row["request_id"])).fetchone()
            if previous is not None and previous["payload_digest"] != digest:
                raise ValueError("Conflicting accepted message during managed route repair")
            if previous is None:
                conn.execute("""INSERT INTO session_admissions(admission_id,request_id,principal_id,
                    target_session_id,lineage_json,payload_json,payload_digest,intent,status,owner_epoch)
                    VALUES(?,?,?,?,?,?,?,?,'queued',?)""", (uuid.uuid4().hex, row["request_id"], row["principal_id"],
                    home_id, json.dumps([home_id]), row["payload_json"], digest, row["intent"], row["owner_epoch"]))
            conn.execute("UPDATE session_admissions SET status='terminal',outcome='cancelled' WHERE admission_id=?",
                         (row["admission_id"],))
            source = source or candidate
    return source


def _restore_main_route(conn, home_id, source, scope):
    from gateway.session import SessionEntry

    now = datetime.now()
    entry = SessionEntry(HOME_SESSION_KEY, home_id, now, now, origin=source,
                         platform=source.platform, display_name=HOME_SESSION_TITLE)
    conn.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES(?,?,?,?)",
                 (scope, HOME_SESSION_KEY, json.dumps(entry.to_dict()), time.time()))
    conn.execute("UPDATE sessions SET source=?,session_key=?,user_id=?,chat_id=?,chat_type=?,origin_json=?,"
                 "ended_at=NULL,end_reason=NULL WHERE id=?", (source.platform.value, HOME_SESSION_KEY,
                 source.user_id, source.chat_id, source.chat_type, json.dumps(source.to_dict()), home_id))


def repair_local_home_routes(db):
    """Run before process children bind their authorities; the SQLite repair is atomic and idempotent."""
    from gateway.config import load_gateway_config
    from gateway.session import SessionSource, build_session_key
    from hermes_runtime.adapters.state import current_home_session_id
    from hermes_state_local import POLICY_PREFIX

    home_id = current_home_session_id()
    scope = str(load_gateway_config().sessions_dir.resolve())

    def write(conn):
        repaired = 0
        for row in conn.execute("SELECT key,value FROM state_meta WHERE key GLOB ?",
                                (POLICY_PREFIX + "*",)).fetchall():
            receipt = json.loads(row["value"])
            if receipt.get("route") != HOME_SESSION_KEY:
                continue
            source = SessionSource.from_dict(receipt["entry"]["origin"])
            from gateway.config import Platform
            if source.platform != Platform.LOCAL:
                continue
            route = build_session_key(source, profile=source.profile)
            lineage = receipt.get("lineage", [receipt["session_id"]])
            _move_local_index(conn, lineage, route)
            receipt["route"] = receipt["entry"]["session_key"] = route
            conn.execute("UPDATE state_meta SET value=? WHERE key=?", (json.dumps(receipt), row["key"]))
            conn.executemany("UPDATE sessions SET session_key=? WHERE id=? AND session_key=?",
                             [(route, sid, HOME_SESSION_KEY) for sid in lineage])
            recovered_source = _move_unstarted_messages(conn, lineage, home_id)
            if recovered_source is not None:
                _restore_main_route(conn, home_id, recovered_source, scope)
            repaired += 1
        return repaired

    repaired = db._execute_write(write)
    if repaired:
        logging.getLogger(__name__).info("Repaired %s local route collision(s); history and accepted work retained", repaired)
    return repaired
