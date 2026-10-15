"""Exercise discovery, durable background ingestion, recall and real native tool dispatch."""

import json
import threading
from types import SimpleNamespace
import pytest

from agent.memory_manager import MemoryManager
from plugins.memory import load_memory_provider
from plugins.memory.holographic.consolidation import Consolidator


def _manager(home, session="conversation"):
    config = home / "config.yaml"
    if not config.exists():
        config.write_text("plugins:\n  hermes-memory-store:\n    consolidation_enabled: true\n")
    manager = MemoryManager()
    provider = load_memory_provider("holographic")
    manager.add_provider(provider)
    manager.initialize_all(session_id=session, hermes_home=str(home))
    return manager, provider


def test_background_corrections_retry_after_restart_without_learning_summaries(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(a))
    started, release = threading.Event(), threading.Event()
    fail = [False]

    def extract(**request):
        assert request["task"] == "memory_consolidation"
        payload = json.loads(request["messages"][-1]["content"])
        started.set()
        assert release.wait(timeout=10)
        source = payload["sources"][-1]
        if fail[0]:
            facts = [{"content": "Invented", "key": "project.database",
                      "source_id": source["source_id"], "quote": "not in evidence"}]
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=json.dumps({"facts": facts})))])
        content = "Проект использует SQLite" if "SQLite" in source["quote"] else "Проект использует PostgreSQL"
        fact = {"content": content, "key": "project.database", "category": "project",
                "source_id": source["source_id"], "quote": source["quote"]}
        if payload["current_facts"]:
            fact["fact_id"] = payload["current_facts"][0]["fact_id"]
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"facts": [fact]})))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", extract)
    manager, provider = _manager(a)
    first = {"role": "user", "content": "Для проекта используем PostgreSQL", "_row_id": 10}
    correction = {"role": "user", "content": '[AgentReady event]\n{"source":"chat","event_id":"correction"}\n\n'
                  "Исправление: для проекта используем SQLite", "_row_id": 20}
    try:
        manager.sync_all(first["content"], "ok", session_id="conversation", messages=[first])
        assert started.wait(timeout=10)  # the caller is free while the extraction waits
        assert provider._store.list_facts() == []
        release.set()
        assert manager.flush_pending(timeout=10)
        fact_id = provider._store.list_facts()[0]["fact_id"]
        fail[0] = True
        manager.sync_all(correction["content"], "ok", session_id="conversation", messages=[first, correction])
        assert manager.flush_pending(timeout=10)
        assert len(provider._store.fact_history(fact_id)) == 1
        assert provider._store._one("SELECT COUNT(*) FROM memory_jobs WHERE completed=0")[0] == 1
    finally:
        manager.shutdown_all()

    # Failed jobs survive closing all connections, then retry on the next completed turn.
    fail[0] = False
    manager, provider = _manager(a)
    try:
        from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
        summary = {"role": "user", "content": "We decided to use MySQL", COMPRESSED_SUMMARY_METADATA_KEY: True}
        manager.sync_all("продолжай", "ok", session_id="conversation", messages=[first, correction, summary])
        assert manager.flush_pending(timeout=10)
        recalled = manager.prefetch_all("какую базу использует проект SQLite?", session_id="conversation")
        assert "SQLite" in recalled and "PostgreSQL" not in recalled and "MySQL" not in recalled
        history = json.loads(manager.handle_tool_call("fact_store", {"action": "history", "fact_id": fact_id}))["versions"]
        assert [h["sources"][0]["message_id"] for h in history] == ["10", "20"]
        assert history[-1]["sources"][0]["channel"] == "chat"
        assert history[-1]["sources"][0]["quote"] == "Исправление: для проекта используем SQLite"
        assert len(history) == 2
        # A→B→A: the provider keeps the owner passed at initialization, even off-turn.
        monkeypatch.setenv("HERMES_HOME", str(b))
        other, other_provider = _manager(b)
        try:
            assert other.prefetch_all("SQLite project", session_id="conversation") == ""
            assert other_provider._store.db_path != provider._store.db_path
        finally:
            other.shutdown_all()
        monkeypatch.setenv("HERMES_HOME", str(a))
        assert "SQLite" in manager.prefetch_all("проект SQLite", session_id="conversation")
    finally:
        manager.shutdown_all()


def test_native_loop_recalls_memory_and_records_actual_tool_evidence(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("""
model:
  provider: custom
  default: gpt-4.1
  base_url: http://127.0.0.1:9/v1
compression:
  enabled: false
plugins:
  hermes-memory-store:
    consolidation_enabled: false
""")
    manager, provider = _manager(tmp_path)
    fact_id = provider._store.add_fact("Проект использует PostgreSQL", key="project.database",
                                      sources=[{"kind": "conversation", "session_id": "conversation",
                                                "message_id": "1", "quote": "используем PostgreSQL"}])
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append(payload["messages"])
        tool_calls = None
        if len(seen) == 1:
            assert "project.database" in json.dumps(payload["messages"], ensure_ascii=False)
            tool_calls = [{"id": "correct-fact", "type": "function", "function": {"name": "fact_store",
                "arguments": json.dumps({"action": "update", "fact_id": fact_id,
                    "content": "Проект использует SQLite", "source_quote": "Теперь используем SQLite"})}}]
        message = {"role": "assistant", "content": None if tool_calls else "Принято."}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return ChatCompletion(id="test-memory", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", source="cli")
    db.set_session_title("conversation", "Memory integration")
    agent = AIAgent(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1", api_key="test-only",
        api_mode="chat_completions", session_id="conversation", session_db=db, memory_manager=manager,
        enabled_toolsets=["memory"], skip_context_files=True, load_soul_identity=False, quiet_mode=True)
    try:
        result = agent.run_conversation("Теперь используем SQLite вместо PostgreSQL")
        assert result.get("final_response") == "Принято."
        current = json.loads(manager.handle_tool_call("fact_store", {"action": "explain", "fact_id": fact_id}))["results"][0]
        assert current["content"] == "Проект использует SQLite"
        assert current["sources"][0]["quote"] == "Теперь используем SQLite"
        assert current["sources"][0]["session_id"] == "conversation"
        assert current["sources"][0]["kind"] == "conversation"
        bad = json.loads(manager.handle_tool_call("fact_store", {"action": "update", "fact_id": fact_id,
            "content": "invented", "source_quote": "not present"},
            messages=[{"role": "user", "content": "real text"}], session_id="conversation"))
        assert bad.get("error")
        # Recall is appended turn context; it never rewrites the cached system-prompt prefix.
        assert seen[0][0]["content"] == seen[1][0]["content"]
        assert "SQLite" in manager.prefetch_all("какая база проекта SQLite", session_id="conversation")
    finally:
        agent.close()
        manager.shutdown_all()
        db.close()


@pytest.mark.parametrize("failure_before_retry", [False, True])
def test_late_consolidation_cannot_replace_a_newer_manual_correction(tmp_path, monkeypatch, failure_before_retry):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager, provider = _manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    fact_id = provider._store.add_fact("Project uses PostgreSQL", key="project.database")
    failed = [failure_before_retry]

    def delayed(self, payload):
        entered.set()
        assert release.wait(timeout=10)
        source = payload["sources"][0]
        return {"facts": [{"fact_id": fact_id, "content": "Project uses SQLite",
            "category": "project", "key": "project.database", "source_id": source["source_id"],
            "quote": "fabricated quote" if failed[0] else source["quote"]}]}

    monkeypatch.setattr(Consolidator, "complete", delayed)
    try:
        manager.sync_all("Project now uses SQLite", "ok", session_id="conversation")
        assert entered.wait(timeout=10)
        provider._store.update_fact(fact_id, content="Project now uses MariaDB",
            sources=[{"kind": "conversation", "message_id": "later", "quote": "Use MariaDB instead"}])
        release.set()
        assert manager.flush_pending(timeout=10)
        if failure_before_retry:
            assert provider._store._one("SELECT completed FROM memory_jobs")[0] == 0
            failed[0] = False
            manager.sync_all("continue", "ok", session_id="conversation", messages=[])
            assert manager.flush_pending(timeout=10)
        assert provider._store.list_facts()[0]["content"] == "Project now uses MariaDB"
        assert [v["content"] for v in provider._store.fact_history(fact_id)] == [
            "Project uses PostgreSQL", "Project now uses MariaDB"]
        assert provider._store._one("SELECT completed FROM memory_jobs")[0] == 1
    finally:
        release.set()
        manager.shutdown_all()
