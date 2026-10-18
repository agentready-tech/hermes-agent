"""Native model loops exchange questions/replies while the same delegated tasks are running."""

import json
import sqlite3
import threading

from openai.types.chat import ChatCompletion

from tests.tools.test_delegate_projects import _parent
from tests.waiting import wait_until


def _response(text=None, args=None):
    message = {"role": "assistant", "content": text}
    if args is not None:
        message["tool_calls"] = [{"id": "call_" + str(hash(json.dumps(args, sort_keys=True))), "type": "function",
                                  "function": {"name": "delegate_task", "arguments": json.dumps(args)}}]
    return ChatCompletion(id="mail-live", object="chat.completion", created=1, model="gpt-4.1",
        choices=[{"index": 0, "message": message, "finish_reason": "tool_calls" if args else "stop"}],
        usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})


def test_live_leaf_parent_and_siblings_reply_before_finishing(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_projects

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("auxiliary:\n  title_generation:\n    enabled: false\ncompression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\n  max_spawn_depth: 1\n")
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": True})
    asked, done = threading.Event(), threading.Event()
    rounds, traces = {}, {}

    def project_id(key):
        with sqlite3.connect(tmp_path / "state.db") as conn:
            return conn.execute("SELECT subagent_id FROM delegated_projects WHERE project_key=?", (key,)).fetchone()[0]

    def model(self, payload, *args, **kwargs):
        sid = self.session_id
        rounds[sid] = rounds.get(sid, 0) + 1
        text = json.dumps(payload["messages"])
        traces.setdefault(sid, []).append(payload["messages"])
        assert "delegate_task" in self.valid_tool_names
        if sid == "main":
            if "ASK_DEADLINE" in text and "USE_MONDAY" not in text:
                with sqlite3.connect(tmp_path / "state.db") as conn:
                    assert conn.execute("SELECT state FROM delegated_project_messages WHERE subagent_id=? ORDER BY rowid LIMIT 1", (project_id("supply"),)).fetchone()[0] == "running"
                return _response(args={"action": "message", "subagent_id": project_id("supply"), "message": "USE_MONDAY"})
            if "STATUS_STILL_WORKING" in text and "CONTINUE_B" not in text:
                return _response(args={"action": "message", "subagent_id": project_id("delivery"), "message": "CONTINUE_B"})
            if done.is_set():
                return _response("Parent coordinated the ongoing work")
            return _response(args={"action": "wait", "timeout_seconds": 1})
        assert self._delegate_role == "leaf"
        if sid == project_id("supply"):
            if rounds[sid] == 1:
                return _response(args={"action": "message", "subagent_id": "parent", "message": "ASK_DEADLINE"})
            if rounds[sid] == 2:
                asked.set()
                return _response(args={"action": "message", "subagent_id": project_id("delivery"), "message": "NEED_B_FINDING"})
            if "USE_MONDAY" in text and "B_FINDING_REPLY" in text:
                return _response("SUPPLY_FINISHED_AFTER_BOTH_REPLIES")
            return _response(args={"action": "wait", "timeout_seconds": 1})
        if "NEED_B_FINDING" in text and "B_FINDING_REPLY" not in text:
            return _response(args={"action": "message", "subagent_id": project_id("supply"), "message": "B_FINDING_REPLY"})
        if "B_FINDING_REPLY" in text and "STATUS_STILL_WORKING" not in text:
            return _response(args={"action": "message", "subagent_id": "parent", "message": "STATUS_STILL_WORKING"})
        if "CONTINUE_B" in text:
            done.set()
            return _response("DELIVERY_FINISHED_AFTER_PARENT_REPLY")
        return _response(args={"action": "wait", "timeout_seconds": 1})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    try:
        dispatched = json.loads(parent._dispatch_delegate_task({"tasks": [
            {"project": "supply", "goal": "Analyse the supplier terms"},
            {"project": "delivery", "goal": "Analyse the delivery terms"},
        ]}))
        assert not dispatched.get("error"), dispatched
        assert asked.wait(timeout=15)
        result = parent.run_conversation("Coordinate the existing project work and answer its questions")
        assert "coordinated" in result["final_response"], result

        def finished():
            with sqlite3.connect(tmp_path / "state.db") as conn:
                return conn.execute("SELECT COUNT(*) FROM delegated_project_messages WHERE state='completed'").fetchone()[0] == 2

        wait_until(finished, timeout=15, what="same delegated tasks finish after replies")
        with sqlite3.connect(tmp_path / "state.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM delegation_mail WHERE delivered_at IS NOT NULL").fetchone()[0] == 6
        for requests in traces.values():
            assert all(request[0] == requests[0][0] for request in requests)
    finally:
        from tools.async_delegation import interrupt_for_session, has_live_for_session
        interrupt_for_session(parent_session_id=parent.session_id)
        wait_until(lambda: not has_live_for_session(parent_session_id=parent.session_id), timeout=15, what="child teardown")
        parent.close()
        db.close()


def test_mail_arriving_during_final_model_call_continues_same_turn(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools.delegate_project_store import ProjectStore
    from tools.delegate_mail_store import MailStore
    from types import SimpleNamespace

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    parent, db = _parent(tmp_path)
    with ProjectStore(parent) as store:
        project = store.create("supply", "Investigate supply terms", None)
    actor = SimpleNamespace(session_id=project["session_id"], _session_db=db)
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append(payload["messages"])
        if len(seen) == 1:
            with MailStore(actor) as mail:
                mail.send("parent", "LATE_EVIDENCE_MONDAY")
            return _response("Tentative result")
        assert "LATE_EVIDENCE_MONDAY" in json.dumps(payload["messages"])
        return _response("Updated result uses Monday")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    try:
        result = parent.run_conversation("Give the result")
        assert result["final_response"] == "Updated result uses Monday", result
        assert len(seen) == 2 and seen[0][0] == seen[1][0]
        with sqlite3.connect(tmp_path / "state.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM delegation_mail WHERE delivered_at IS NOT NULL AND content=''").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE platform_message_id LIKE 'agent-mail-delivery-%'").fetchone()[0] == 1
    finally:
        parent.close()
        db.close()


def test_synchronous_join_yields_for_child_question_and_then_continues(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_projects

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  max_spawn_depth: 1\n  worktree_isolation: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": False})
    calls = {}

    def model(self, payload, *args, **kwargs):
        sid = self.session_id
        calls[sid] = calls.get(sid, 0) + 1
        text = json.dumps(payload["messages"])
        if sid == "main":
            if calls[sid] == 1:
                return _response(args={"goal": "Inspect this and ask the coordinator for its deadline"})
            if "SYNC_QUESTION" in text and "SYNC_ANSWER" not in text:
                with sqlite3.connect(tmp_path / "state.db") as conn:
                    child = conn.execute("SELECT subagent_id FROM delegated_projects").fetchone()[0]
                return _response(args={"action": "message", "subagent_id": child, "message": "SYNC_ANSWER"})
            if "finished input" in text:
                return _response("ROOT_DONE")
            return _response(args={"action": "wait", "timeout_seconds": 1})
        if calls[sid] == 1:
            return _response(args={"action": "message", "subagent_id": "parent", "message": "SYNC_QUESTION"})
        if "SYNC_ANSWER" in text:
            return _response("CHILD_DONE")
        return _response(args={"action": "wait", "timeout_seconds": 1})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    try:
        result = parent.run_conversation("Delegate and resolve the child's question before finishing")
        assert result["final_response"] == "ROOT_DONE", result
        with sqlite3.connect(tmp_path / "state.db") as conn:
            receipt = conn.execute("SELECT state,result_json FROM delegated_project_messages").fetchone()
            assert receipt[0] == "completed" and json.loads(receipt[1])["summary"] == "CHILD_DONE"
            assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 1
    finally:
        parent.interrupt("test teardown")
        parent.close()
        db.close()
