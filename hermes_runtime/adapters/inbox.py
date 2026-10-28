"""Receipt-before-ack for the managed Unix mailbox, in the existing state.db."""

import json
import time
import uuid

from hermes_runtime.adapters.hermes import managed_home


class HomeInbox:
    def __init__(self, home=None):
        self.home = home if home is not None else managed_home()

    def transaction(self):
        from hermes_cli.sqlite_util import open_db, transaction
        from hermes_state import _secure_state_db_files
        from tools.async_delegation import _initialize_schema
        path = self.home / "state.db"
        _secure_state_db_files(path, create_main=True)
        return transaction(open_db(path, db_label="state.db (managed inbox)", wal=False,
                                   busy_timeout_ms=10000, initialize=_initialize_schema), immediate=True)

    def enqueue(self, content):
        key = None
        text = content if isinstance(content, str) else ""
        if text.startswith("[AgentReady event]\n"):
            try:
                event = json.loads(text.split("\n", 1)[1].split("\n\n", 1)[0])
                if event.get("event_id"):
                    key = json.dumps([event.get("source"), event["event_id"]])
            except (ValueError, TypeError):
                pass  # Plain content remains a valid, individually receipted message.
        identity = "inbox-" + uuid.uuid4().hex
        with self.transaction() as conn:
            floor = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO agentready_inbox(message_id,event_key,content,accepted_at,accepted_after_row_id) VALUES (?,?,?,?,?)",
                         (identity, key, json.dumps(content, ensure_ascii=False), time.time(), floor))
            row = conn.execute("SELECT message_id,delivered_at FROM agentready_inbox WHERE " + ("event_key=?" if key else "message_id=?"), (key or identity,)).fetchone()
            return row[0], row[1] is None

    def pending(self):
        with self.transaction() as conn:
            rows = conn.execute("SELECT message_id,content FROM agentready_inbox WHERE delivered_at IS NULL ORDER BY accepted_at,message_id").fetchall()
            return [(r[0], json.loads(r[1])) for r in rows]

    def acknowledge_recorded(self, identities=None):
        with self.transaction() as conn:
            rows = conn.execute("SELECT message_id,accepted_after_row_id FROM agentready_inbox WHERE delivered_at IS NULL").fetchall()
            for identity, floor in rows:
                if identities is not None and identity not in identities:
                    continue
                # UUID receipts travel as native persistence-only metadata. Text
                # matching could retire a second, identical but unadmitted input.
                recorded = conn.execute("SELECT 1 FROM messages WHERE role='user' AND platform_message_id=? LIMIT 1", (identity,)).fetchone()
                if not recorded:
                    candidates = conn.execute("SELECT display_metadata FROM messages WHERE role='user' AND id>? AND display_metadata IS NOT NULL", (floor,)).fetchall()
                    recorded = any(identity in json.loads(row[0]).get("agentready_inbox_ids", []) for row in candidates)
                if recorded:
                    conn.execute("UPDATE agentready_inbox SET delivered_at=?,content='null' WHERE message_id=?", (time.time(), identity))
