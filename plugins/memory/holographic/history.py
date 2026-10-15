"""Additive fact revisions and evidence. The facts table remains the current projection."""

import json
from contextlib import contextmanager


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    tags TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    previous_version_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    valid_until TEXT
);
CREATE INDEX IF NOT EXISTS fact_versions_fact ON fact_versions(fact_id, version_id);
CREATE UNIQUE INDEX IF NOT EXISTS fact_versions_current ON fact_versions(fact_id) WHERE valid_until IS NULL;
CREATE TABLE IF NOT EXISTS fact_keys (key TEXT PRIMARY KEY, fact_id INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS memory_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    source_json TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS memory_consolidation_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_forgotten_sources (event_key TEXT PRIMARY KEY);
"""


class FactHistory:
    @contextmanager
    def _mutation(self):
        # One SQLite transaction covers the current projection, entities, vectors and revision.
        with self._lock:
            owner = not self._conn.in_transaction
            if owner:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                if owner:
                    self._conn.commit()
            except BaseException:
                if owner:
                    self._conn.rollback()
                raise

    def _init_history(self):
        self._conn.executescript(_SCHEMA)
        with self._mutation():
            self._conn.execute("""INSERT INTO fact_versions(fact_id,content,category,tags,sources_json)
                SELECT fact_id,content,COALESCE(category,'general'),COALESCE(tags,''),'[{"kind":"legacy","quote":""}]' FROM facts f
                WHERE NOT EXISTS (SELECT 1 FROM fact_versions v WHERE v.fact_id=f.fact_id)""")

    def _current_version(self, fact_id):
        return self._one("SELECT * FROM fact_versions WHERE fact_id=? AND valid_until IS NULL", (fact_id,))

    def _record_version(self, fact_id, sources=None):
        previous = self._current_version(fact_id)
        fact = self._one("SELECT * FROM facts WHERE fact_id=?", (fact_id,))
        if sources is None:
            sources = [{"kind": "agent_record", "quote": ""}]
        if not isinstance(sources, list) or not sources or len(sources) > 16:
            raise ValueError("Provide between one and sixteen evidence sources")
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
                raise ValueError("Each evidence source needs a kind")
        payload = json.dumps(sources, ensure_ascii=False)
        if len(payload) > 32_000:
            raise ValueError("Evidence exceeds the fact budget")
        if previous:
            self._conn.execute("UPDATE fact_versions SET valid_until=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                               "WHERE version_id=?", (previous["version_id"],))
        self._conn.execute("""INSERT INTO fact_versions
            (fact_id,content,category,tags,sources_json,previous_version_id) VALUES (?,?,?,?,?,?)""",
            (fact_id, fact["content"], fact["category"] or "general", fact["tags"] or "", payload,
             previous["version_id"] if previous else None))

    def _attach_sources(self, fact_id, sources):
        if not sources:
            return
        current = self._current_version(fact_id)
        existing = json.loads(current["sources_json"])
        merged = existing + [source for source in sources if source not in existing]
        self._conn.execute("UPDATE fact_versions SET sources_json=? WHERE version_id=?",
                           (json.dumps(merged[-16:], ensure_ascii=False), current["version_id"]))

    def _forget_fact_sources(self, fact_id):
        rows = self._conn.execute("SELECT sources_json FROM fact_versions WHERE fact_id=?", (fact_id,)).fetchall()
        keys = {s["event_key"] for row in rows for s in json.loads(row[0]) if s.get("event_key")}
        for key in keys:
            self._conn.execute("INSERT OR IGNORE INTO memory_forgotten_sources VALUES (?)", (key,))
            self._conn.execute("UPDATE memory_jobs SET completed=1,source_json='{}' WHERE event_key=?", (key,))

    def fact_history(self, fact_id, limit=20):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM fact_versions WHERE fact_id=? ORDER BY version_id DESC LIMIT ?",
                                      (fact_id, max(1, min(int(limit), 100)))).fetchall()
            return [self._version_dict(row) for row in reversed(rows)]

    @staticmethod
    def _version_dict(row):
        result = dict(row)
        result["sources"] = json.loads(result.pop("sources_json"))
        return result

    def describe_facts(self, facts):
        with self._lock:
            result = []
            for fact in facts:
                revision = self._current_version(fact["fact_id"])
                if revision is None:
                    continue
                key = self._one("SELECT key FROM fact_keys WHERE fact_id=?", (fact["fact_id"],))
                described = {**fact, "version_id": revision["version_id"], "key": key[0] if key else None,
                             "sources": json.loads(revision["sources_json"])}
                described.pop("hrr_vector", None)
                result.append(described)
            return result
