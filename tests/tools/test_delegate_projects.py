"""Project delegates retain native conversations across reuse and interrupted delivery."""

import json
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from tests.waiting import wait_until


def _wait(predicate):
    wait_until(predicate, timeout=15, what="project inbox completion")


def _parent(home):
    from hermes_state import SessionDB
    from run_agent import AIAgent
    db = SessionDB(db_path=home / "state.db")
    db.create_session("main", source="cli")
    parent = AIAgent(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1",
        api_key="test-only", api_mode="chat_completions", session_id="main", session_db=db,
        enabled_toolsets=["delegation"], skip_context_files=True, load_soul_identity=False,
        skip_memory=True, quiet_mode=True)
    return parent, db


def test_project_reuses_native_history_and_recovers_committed_answer(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from tools.delegate_tool import delegate_task

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "Initial"], check=True)
    (tmp_path / "config.yaml").write_text(f"compression:\n  enabled: false\nterminal:\n  cwd: {repo}\ndelegation:\n  worktree_isolation: true\n")
    seen = []
    workspaces = []

    def model(self, payload, *args, **kwargs):
        from tools.terminal_tool import get_session_cwd
        cwd = Path(get_session_cwd(self._current_task_id))
        workspaces.append(cwd)
        if seen:
            assert (cwd / "evidence.txt").read_text() == "Supplier source preserved"
        seen.append((self.session_id, payload["messages"]))
        return ChatCompletion(id="project-test", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": f"Analysis {len(seen)}"},
                      "finish_reason": "stop"}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    progress = []
    parent.tool_progress_callback = lambda event, *args, **kwargs: progress.append((event, kwargs))
    try:
        first = json.loads(delegate_task(tasks=[{"goal": "Analyse supplier delivery for order 42",
            "project": "order-42", "context": "Original delivery date is Friday"}], parent_agent=parent))
        assert not first.get("error"), first
        item = first["projects"][0]
        project_id, session_id = item["subagent_id"], item["session_id"]
        assert session_id != "main"
        assert workspaces[0] != repo
        (workspaces[0] / "evidence.txt").write_text("Supplier source preserved")
        second = json.loads(delegate_task(action="send", subagent_id=project_id,
            message="New email: delivery moved to Monday. Update the analysis.", parent_agent=parent))
        assert not second.get("error"), second
        assert len(seen) == 2
        assert [sid for sid, _ in seen] == [session_id, session_id]
        context = json.dumps(seen[-1][1])
        assert "Friday" in context and "Analysis 1" in context and "Monday" in context
        assert seen[0][1][0]["content"] == seen[1][1][0]["content"]
        assert workspaces[0] == workspaces[1]
        assert progress and {kw["subagent_id"] for _, kw in progress if "subagent_id" in kw} == {project_id}
        listing = json.loads(delegate_task(action="list", parent_agent=parent))
        assert any(p["subagent_id"] == project_id for p in listing["projects"])
        # Crash after the native answer committed but before project completion bookkeeping.
        with sqlite3.connect(tmp_path / "state.db") as conn:
            conn.execute("UPDATE delegated_project_messages SET state='running',owner_pid=-1,owner_started_at=NULL "
                         "WHERE message_id=?", (second["projects"][0]["message_id"],))
    finally:
        parent.close()
        db.close()
    parent, db = _parent(tmp_path)
    try:
        from tools.delegate_projects import recover_projects
        recover_projects(parent)
        assert len(seen) == 2  # the already recorded final answer must not run again
        reply = json.loads(delegate_task(action="send", subagent_id=project_id,
            message="Customer accepted Monday. Continue the same order.", parent_agent=parent))
        assert not reply.get("error"), reply
        assert seen[-1][0] == session_id and len(seen) == 3
        assert "Monday" in json.dumps(seen[-1][1])
        # A different root conversation cannot take over this project's history.
        db.create_session("other", source="cli")
        parent.session_id = "other"
        assert json.loads(delegate_task(action="send", subagent_id=project_id,
            message="steal the conversation", parent_agent=parent)).get("error")
    finally:
        parent.close()
        db.close()


def test_project_inbox_serializes_new_facts_and_deduplicates_retries(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from tools.registry import registry
    from tools import delegate_projects
    from tools.delegate_project_store import ProjectStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\n")
    entered, release = threading.Event(), threading.Event()
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append((self.session_id, payload["messages"]))
        if len(seen) == 1:
            entered.set()
            assert release.wait(timeout=15)
        return ChatCompletion(id="project-async", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": f"Project result {len(seen)}"},
                      "finish_reason": "stop"}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": True, "session_key": "", "origin_ui_session_id": "", "scope_id": "source-at-admission"})
    parent, db = _parent(tmp_path)
    try:
        def call(args):
            result = json.loads(registry.dispatch("delegate_task", args, parent_agent=parent))
            assert not result.get("error"), result
            return result
        first = call({"tasks": [{"project": "order-42", "goal": "Analyse the initial supplier offer"}]})["projects"][0]
        assert entered.wait(timeout=15)
        project_id = first["subagent_id"]
        update = {"action": "send", "subagent_id": project_id, "message": "Email: delivery moves to Monday", "event_id": "mail-1"}
        second = call(update)["projects"][0]
        duplicate = call(update)["projects"][0]
        assert second["message_id"] == duplicate["message_id"]
        third = call({"action": "send", "subagent_id": project_id, "message": "Customer accepts Monday", "event_id": "mail-2"})["projects"][0]
        assert third["message_id"] != second["message_id"]
        assert len(seen) == 1
        # Release the request's own DB/client while independent child handles remain live.
        parent.close()
        db.close()
        release.set()
        def finished():
            with sqlite3.connect(tmp_path / "state.db") as conn:
                return conn.execute("SELECT COUNT(*) FROM delegated_project_messages WHERE state='completed' AND published=1").fetchone()[0] == 3
        _wait(finished)
        assert len(seen) == 3 and len({sid for sid, _ in seen}) == 1
        with sqlite3.connect(tmp_path / "state.db") as conn:
            events = [json.loads(row[0]) for row in conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id LIKE 'project-msg-%'")]
            assert len(events) == 3 and all(e["scope_id"] == "source-at-admission" for e in events)
        assert "Monday" in json.dumps(seen[2][1]) and "Project result 1" in json.dumps(seen[2][1])
        # A -> B -> A, with actual profile databases and no process-local identity reliance.
        other_home = tmp_path / "other"
        other_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(other_home))
        other, other_db = _parent(other_home)
        try:
            with ProjectStore(other) as store:
                assert store.projects() == []
                with pytest.raises(ValueError):
                    store.get(project_id)
        finally:
            other.close()
            other_db.close()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        parent, db = _parent(tmp_path)
        assert call({"action": "read", "subagent_id": project_id})["project"]["session_id"] == first["session_id"]
    finally:
        release.set()
        parent.close()
        db.close()
