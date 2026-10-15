"""Facts retain their evidence and corrections across upgrade, concurrency and restart."""

import sqlite3

import pytest

from plugins.memory.holographic.store import MemoryStore
from plugins.memory.holographic.retrieval import FactRetriever


def test_corrections_keep_sources_and_only_recall_the_current_version(tmp_path):
    path = tmp_path / "memory.db"
    # An actual pre-feature database must remain readable after the additive migration.
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE facts (
            fact_id INTEGER PRIMARY KEY, content TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT 'general', tags TEXT DEFAULT '', trust_score REAL DEFAULT .5,
            retrieval_count INTEGER DEFAULT 0, helpful_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO facts(content) VALUES ('Existing preference');""")
    original = {"session_id": "conversation", "message_id": "10", "role": "user",
                "quote": "The project uses PostgreSQL", "kind": "conversation"}
    correction = {**original, "message_id": "20", "quote": "Correction: the project uses SQLite"}
    with MemoryStore(path, hrr_dim=64) as store:
        assert store.list_facts()[0]["content"] == "Existing preference"
        assert FactRetriever(store, hrr_dim=64).search("Existing preference")
        fact_id = store.add_fact(original["quote"], sources=[original], key="project.database")
        before = store.fact_history(fact_id)
        assert before[-1]["sources"][0]["message_id"] == "10"
        assert store.update_fact(fact_id, content="The project uses SQLite", sources=[correction],
                                 expected_version=before[-1]["version_id"])
        # A worker that read the previous revision cannot publish over the correction.
        with pytest.raises(ValueError, match="changed"):
            store.update_fact(fact_id, content="The project uses MySQL", sources=[original],
                              expected_version=before[-1]["version_id"])
        recalled = FactRetriever(store, hrr_dim=64).search("project database")
        assert [r["content"] for r in recalled] == ["The project uses SQLite"]
        assert store.describe_facts(recalled)[0]["sources"][0]["message_id"] == "20"
    with MemoryStore(path, hrr_dim=64) as store:
        history = store.fact_history(fact_id)
        assert [h["content"] for h in history] == ["The project uses PostgreSQL", "The project uses SQLite"]
        assert history[1]["previous_version_id"] == history[0]["version_id"]
        assert history[0]["sources"][0]["quote"] == original["quote"]
        # Existing remove means forget: neither current facts nor retained revisions can resurrect it.
        assert store.remove_fact(fact_id)
        assert store.fact_history(fact_id) == []
        assert FactRetriever(store, hrr_dim=64).search("PostgreSQL SQLite") == []
