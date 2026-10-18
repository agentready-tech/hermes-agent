"""The actual model requests teach one protocol without replacing saved child prefixes."""

import json
import sqlite3

import pytest

from tests.tools.test_delegate_mail_live import _response
from tests.tools.test_delegate_projects import _parent


def _description(payload):
    return next(tool["function"]["description"] for tool in payload["tools"]
                if tool["function"]["name"] == "delegate_task")


@pytest.mark.parametrize("max_depth", [1, 2])
def test_parent_and_child_requests_share_coordination_rules(tmp_path, monkeypatch, max_depth):
    from run_agent import AIAgent
    from tools import delegate_projects

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"compression:\n  enabled: false\ndelegation:\n  max_spawn_depth: {max_depth}\n  worktree_isolation: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": False})
    seen = {}

    def model(self, payload, *args, **kwargs):
        seen.setdefault(self.session_id, []).append(payload)
        if self.session_id == "main" and len(seen[self.session_id]) == 1:
            return _response(args={"goal": "Inspect the supplier terms"})
        return _response("Verified supplier finding")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    try:
        result = parent.run_conversation("Delegate the supplier analysis")
        assert result["completed"]
        child_id = next(sid for sid in seen if sid != "main")
        for requests in seen.values():
            description = _description(requests[0])
            assert "only its final summary" not in description
            assert "END YOUR TURN" not in description
            assert "blocker" in description and "conflicting" in description
        from tools.delegate_tool_progress import _COORDINATION_INSTRUCTIONS
        guidance = _COORDINATION_INSTRUCTIONS.strip()
        assert guidance in _description(seen["main"][0])
        assert guidance in _description(seen[child_id][0])
        assert guidance in seen[child_id][0]["messages"][0]["content"]
    finally:
        parent.close()
        db.close()


def test_saved_child_gets_current_tool_guidance_without_rewriting_history(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_projects
    from tools.delegate_project_store import ProjectStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": False})
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append((self.session_id, payload))
        return _response("Updated supplier finding")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    legacy = "You are the supplier subagent. Return a concise final summary to your parent."
    try:
        with ProjectStore(parent) as store:
            project = store.create("saved-supplier", "Analyse supplier terms", None)
            store.instruction(project["subagent_id"], legacy)
        sid = project["session_id"]
        db.create_session(sid, source="cli", parent_session_id="main", model_config={"_delegate_from": "main"})
        db.append_message(sid, "user", "Original supplier task")
        db.append_message(sid, "assistant", "Original supplier finding")
        with sqlite3.connect(tmp_path / "state.db") as conn:
            before = conn.execute("SELECT id,role,content,platform_message_id FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        receipt = json.loads(parent._dispatch_delegate_task({"action": "send", "subagent_id": project["subagent_id"],
                                                           "message": "New supplier evidence"}))
        assert not receipt.get("error"), receipt
        assert len(seen) == 1 and seen[0][0] == sid
        assert legacy in seen[0][1]["messages"][0]["content"]
        assert "blocker" in _description(seen[0][1])
        with ProjectStore(parent) as store:
            assert store.instruction(project["subagent_id"], "Must not replace the saved prefix") == legacy
        with sqlite3.connect(tmp_path / "state.db") as conn:
            after = [conn.execute("SELECT id,role,content,platform_message_id FROM messages WHERE id=?", (row[0],)).fetchone() for row in before]
            assert after == before
    finally:
        parent.close()
        db.close()
