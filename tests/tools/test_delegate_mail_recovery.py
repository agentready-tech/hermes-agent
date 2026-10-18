"""Mailbox admission survives process/object loss and reuses native child histories."""

import json
import sqlite3
from types import SimpleNamespace

from tests.tools.test_delegate_projects import _parent
from tests.tools.test_delegate_mail_live import _response


def test_idle_recipient_resumes_with_mail_in_its_first_request(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_projects
    from tools.delegate_mail import mail_action

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": False})
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append((self.session_id, payload["messages"]))
        return _response("Saved finding" if len(seen) == 1 else "Updated finding")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    try:
        entry = json.loads(parent._dispatch_delegate_task({"goal": "Analyse the supplier terms"}))["projects"][0]
        receipt = mail_action(parent, "message", entry["subagent_id"], "NEW_MAIL_EVIDENCE_MONDAY", "mail-42")
        delegate_projects.recover_projects(parent)
        assert seen[1][0] == entry["session_id"]
        assert "NEW_MAIL_EVIDENCE_MONDAY" in json.dumps(seen[1][1])
        assert "Saved finding" in json.dumps(seen[1][1])
        with sqlite3.connect(tmp_path / "state.db") as conn:
            assert conn.execute("SELECT delivered_at IS NOT NULL FROM delegation_mail WHERE message_id=?", (receipt["message_id"],)).fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 1
    finally:
        parent.close()
        db.close()


def test_native_delivery_witness_repairs_crash_before_mail_ack(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools.delegate_project_store import ProjectStore
    from tools.delegate_mail import mail_action
    from tools.delegate_mail_store import MailStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    parent, db = _parent(tmp_path)
    with ProjectStore(parent) as store:
        child = store.create("supplier", "Investigate supplier", None)
    actor = SimpleNamespace(session_id=child["session_id"], _session_db=db)
    receipt = mail_action(actor, "message", "parent", "PRESERVED_EVIDENCE", "source-1")
    monkeypatch.setattr(AIAgent, "_interruptible_api_call", lambda *a, **k: _response("Final result"))
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", lambda *a, **k: _response("Final result"))
    parent.run_conversation("Handle the queued evidence")
    with sqlite3.connect(tmp_path / "state.db") as conn:
        before = conn.execute("SELECT id,content,platform_message_id FROM messages ORDER BY id").fetchall()
        conn.execute("UPDATE delegation_mail SET delivered_at=NULL,content='PRESERVED_EVIDENCE' WHERE message_id=?", (receipt["message_id"],))
    parent.close()
    db.close()
    parent, db = _parent(tmp_path)
    try:
        assert mail_action(parent, "wait", timeout_seconds=0)["status"] == "timeout"
        with MailStore(parent) as store:
            assert store.pending() == []
        with sqlite3.connect(tmp_path / "state.db") as conn:
            assert conn.execute("SELECT id,content,platform_message_id FROM messages ORDER BY id").fetchall() == before
            assert conn.execute("SELECT content FROM delegation_mail WHERE message_id=?", (receipt["message_id"],)).fetchone()[0] == ""
    finally:
        parent.close()
        db.close()


def test_replayed_question_is_answered_before_joining_its_waiting_child(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from tools import delegate_projects
    from tools.delegate_project_store import ProjectStore
    from tools.delegate_project_resume import resume_input

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent, db = _parent(tmp_path)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def waiting_child(*_):
        started.set()
        try:
            assert release.wait(5), "test did not release the waiting child"
            return []
        finally:
            stopped.set()

    monkeypatch.setattr(delegate_projects, "_recover_project_scoped", waiting_child)
    with ProjectStore(parent) as store:
        store.create("waiting-child", "Waiting for the parent's answer", None)
    db.append_message("main", "user", "Question admitted just before the crash", display_kind="agent_message")
    parent._delegate_resume_kwargs = {}
    resume_input(parent, db.get_messages_as_conversation("main"), "Original task")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        resumed = pool.submit(delegate_projects.recover_project_work, parent)
        assert started.wait(2)
        # This must yield to the parent's LLM; the child is still waiting for its reply.
        assert resumed.result(timeout=1) is None
        assert not stopped.is_set()
    finally:
        release.set()
        pool.shutdown(wait=True)
        assert stopped.wait(2)
        parent.close()
        db.close()
