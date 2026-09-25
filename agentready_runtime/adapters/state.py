"""Hermes SQLite home-session routing and legacy lineage migration."""

import sqlite3
from agentready_runtime.sessions import (
    HOME_SESSION_ID,
    HOME_SESSION_TITLE,
)
from agentready_runtime.adapters.hermes import managed_home


def state_db_path():
    return managed_home() / "state.db"


def find_home_tip(connection):
    row = connection.execute(
        """WITH RECURSIVE lineage(id, depth, visited) AS (
               SELECT id, 0, ',' || id || ',' FROM sessions WHERE id = ?
               UNION ALL
               SELECT child.id, lineage.depth + 1, lineage.visited || child.id || ','
               FROM sessions child
               JOIN sessions parent ON child.parent_session_id = parent.id
               JOIN lineage ON parent.id = lineage.id
               WHERE parent.end_reason = 'compression'
                 AND instr(lineage.visited, ',' || child.id || ',') = 0
           )
           SELECT lineage.id
           FROM lineage JOIN sessions ON sessions.id = lineage.id
           ORDER BY lineage.depth DESC,
                    CASE WHEN sessions.ended_at IS NULL THEN 0 ELSE 1 END,
                    sessions.started_at DESC
           LIMIT 1""",
        (HOME_SESSION_ID,),
    ).fetchone()
    return str(row[0]) if row else HOME_SESSION_ID


def ensure_home_head():
    with sqlite3.connect(state_db_path()) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS agentready_session_heads (
                   logical_session_id TEXT PRIMARY KEY,
                   current_session_id TEXT NOT NULL,
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   FOREIGN KEY(current_session_id) REFERENCES sessions(id)
               )"""
        )
        row = connection.execute(
            "SELECT current_session_id FROM agentready_session_heads WHERE logical_session_id = ?",
            (HOME_SESSION_ID,),
        ).fetchone()
        valid = (
            row
            and connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (row[0],)
            ).fetchone()
        )
        if not valid:
            tip = find_home_tip(connection)
            connection.execute(
                """INSERT INTO agentready_session_heads(logical_session_id, current_session_id, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(logical_session_id) DO UPDATE SET
                     current_session_id = excluded.current_session_id,
                     updated_at = CURRENT_TIMESTAMP""",
                (HOME_SESSION_ID, tip),
            )
        connection.commit()


def current_home_session_id():
    try:
        with sqlite3.connect(state_db_path()) as connection:
            row = connection.execute(
                "SELECT current_session_id FROM agentready_session_heads WHERE logical_session_id = ?",
                (HOME_SESSION_ID,),
            ).fetchone()
            return str(row[0]) if row else HOME_SESSION_ID
    except Exception:
        return HOME_SESSION_ID


def ensure_home_session():
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        if db.get_session(HOME_SESSION_ID) is None:
            db.create_session(
                session_id=HOME_SESSION_ID,
                source="agentready",
                cwd=str(managed_home() / "workspace"),
            )
        db.set_session_title(HOME_SESSION_ID, HOME_SESSION_TITLE)
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()
    ensure_home_head()


def belongs_to_home_lineage(session_id):
    value = str(session_id or "")
    if not value:
        return False
    if value == HOME_SESSION_ID:
        return True
    try:
        with sqlite3.connect(state_db_path()) as connection:
            row = connection.execute(
                """WITH RECURSIVE ancestors(id, parent_session_id) AS (
                       SELECT id, parent_session_id FROM sessions WHERE id = ?
                       UNION ALL
                       SELECT sessions.id, sessions.parent_session_id
                       FROM sessions JOIN ancestors ON sessions.id = ancestors.parent_session_id
                   )
                   SELECT 1 FROM ancestors WHERE id = ? LIMIT 1""",
                (value, HOME_SESSION_ID),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def resolve_home(session_id):
    head = current_home_session_id()
    if session_id in (HOME_SESSION_ID, head) or belongs_to_home_lineage(session_id):
        return head
    return None
