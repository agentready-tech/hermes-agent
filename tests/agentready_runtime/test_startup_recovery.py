"""A real managed WebSocket backend resumes an interrupted home without a human client."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


def _crash_project_process(home, phase="tool"):
    script = '''
import os, time
from pathlib import Path
from openai.types.chat import ChatCompletion
from run_agent import AIAgent
from hermes_state import SessionDB
from tools import delegate_projects
from tools.delegate_tool import delegate_task
home = Path(os.environ["HERMES_HOME"])
if os.environ["PROJECT_CRASH_PHASE"] != "tool":
    import tools.async_delegation
    persist_completion = tools.async_delegation._persist_completion
    def crash_after_answer(*args, **kwargs):
        if os.environ["PROJECT_CRASH_PHASE"] == "outbox":
            persist_completion(*args, **kwargs)
        os._exit(23)
    tools.async_delegation._persist_completion = crash_after_answer
(home / "supplier.txt").write_text("Verified supplier email: delivery is Monday")
db = SessionDB(db_path=home / "state.db")
db.append_message("agentready_home", "user", "Manage supplier order 42")
db.append_message("agentready_home", "assistant", "I will delegate the order analysis")
def model(self, payload, *args, **kwargs):
    if any(m.get("role") == "tool" for m in payload["messages"]):
        if os.environ["PROJECT_CRASH_PHASE"] == "tool":
            os._exit(23)
        return ChatCompletion(id="recorded-answer", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index":0,"message":{"role":"assistant","content":"Already verified delivery: Monday"},"finish_reason":"stop"}],
            usage={"prompt_tokens":100,"completion_tokens":10,"total_tokens":110})
    return ChatCompletion(id="before-crash", object="chat.completion", created=1, model="gpt-4.1",
        choices=[{"index": 0, "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "supplier-read", "type": "function", "function": {
                "name": "read_file", "arguments": __import__("json").dumps({"path": str(home / "supplier.txt")})}}]},
            "finish_reason": "tool_calls"}], usage={"prompt_tokens":100,"completion_tokens":10,"total_tokens":110})
AIAgent._interruptible_api_call = model
AIAgent._interruptible_streaming_api_call = model
delegate_projects._origin = lambda: {"wake": True, "session_key": "", "origin_ui_session_id": ""}
parent = AIAgent(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1", api_key="test-only",
    api_mode="chat_completions", session_id="agentready_home", session_db=db,
    enabled_toolsets=["delegation", "file"], skip_context_files=True, load_soul_identity=False,
    skip_memory=True, quiet_mode=True)
delegate_task(tasks=[{"project":"order-42", "goal":"Analyse the supplier delivery for order 42"}], parent_agent=parent)
while True:
    time.sleep(0.1)
'''
    run = subprocess.run([sys.executable, "-c", script], env={**os.environ, "HERMES_HOME": str(home), "PROJECT_CRASH_PHASE": phase},
                         capture_output=True, text=True, timeout=25)
    assert run.returncode == 23, run.stdout[-2000:] + run.stderr[-2000:]
    with sqlite3.connect(home / "state.db") as conn:
        if phase == "outbox":
            conn.execute("UPDATE async_delegations SET completed_at=?,dispatched_at=?", (time.time()-7*86400, time.time()-7*86400))
        return conn.execute("SELECT session_id FROM delegated_projects WHERE project_key='order-42'").fetchone()[0]


@pytest.mark.parametrize("work", ["home", "project", "receipt", "outbox", "inbox"])
def test_boot_resumes_interrupted_home_through_native_websocket(monkeypatch, work):
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from agentready_runtime.adapters import hermes
    from agentready_runtime.adapters.state import ensure_home_session
    from tui_gateway.turn_marker import record_turn_start, read_turn_marker
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="hb-") as directory:
        home = Path(directory)
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setenv("OPENAI_API_KEY", "test-only")
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(hermes, "_home", home)
        (home / "config.yaml").write_text("model:\n  provider: custom\n  default: gpt-4.1\n  base_url: http://127.0.0.1:9/v1\ncompression:\n  enabled: false\n")
        ensure_home_session()
        project_sid = None
        if work == "home":
            record_turn_start(home, "agentready_home", "Finish the interrupted delivery analysis")
            # A long outage must not turn a managed home into a forgotten desktop tab.
            marker_file = home / "desktop/interrupted_turns.json"
            marker = json.loads(marker_file.read_text())
            marker["agentready_home"]["started_at"] = time.time() - 2 * 24 * 3600
            marker_file.write_text(json.dumps(marker))
        elif work in ("project", "receipt", "outbox"):
            project_sid = _crash_project_process(home, "tool" if work == "project" else work)
        else:
            from agentready_runtime.adapters.inbox import HomeInbox
            HomeInbox().enqueue("Supplier changed delivery to Monday")
        calls = []

        def model(self, payload, *args, **kwargs):
            calls.append((self.session_id, payload["messages"]))
            if work == "project" and self.session_id == project_sid:
                assert "Verified supplier email: delivery is Monday" in json.dumps(payload["messages"])
            return ChatCompletion(id="boot-test", object="chat.completion", created=1, model="gpt-4.1",
                choices=[{"index": 0, "message": {"role": "assistant", "content": "Recovered delivery analysis"},
                          "finish_reason": "stop"}],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
        monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
        monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
        from hermes_cli import web_server
        from tui_gateway import server as gateway
        monkeypatch.setattr(gateway, "_hermes_home", home)
        from hermes_state_registry import release_or_close
        if gateway._db is not None:
            release_or_close(gateway._db)
        monkeypatch.setattr(gateway, "_db", None)
        monkeypatch.setattr(gateway, "_db_error", None)
        backend = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=0, log_level="error"))
        thread = threading.Thread(target=backend.run, daemon=True)
        thread.start()
        recovery = None
        try:
            deadline = time.monotonic() + 20
            while not backend.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert backend.started
            port = backend.servers[0].sockets[0].getsockname()[1]
            from agentready_runtime.adapters.startup import start_recovery
            recovery = start_recovery(port, web_server._SESSION_TOKEN)
            assert recovery is not None
            recovery.join(timeout=20)
            assert not recovery.is_alive(), {
                "calls": len(calls),
                "sessions": [(sid, s.get("session_key"), s.get("running"), s.get("agent_ready").is_set(),
                              s.get("_notif_stop").is_set() if s.get("_notif_stop") else None)
                             for sid, s in gateway._sessions.items()],
                "pending": __import__("agentready_runtime.adapters.inbox", fromlist=["HomeInbox"]).HomeInbox().pending(),
            }
            if work == "home":
                assert len(calls) == 1
                assert "Finish the interrupted delivery analysis" in json.dumps(calls[0])
            elif work in ("project", "receipt", "outbox"):
                assert len([sid for sid, _ in calls if sid == project_sid]) == (1 if work == "project" else 0)
                if work != "project":
                    assert "Already verified delivery: Monday" in json.dumps(calls)
                with sqlite3.connect(home / "state.db") as conn:
                    assert conn.execute("SELECT state FROM delegated_project_messages").fetchone()[0] == "completed"
                    assert conn.execute("SELECT session_id FROM delegated_projects").fetchone()[0] == project_sid
            else:
                assert "Supplier changed delivery to Monday" in json.dumps(calls)
                from agentready_runtime.adapters.inbox import HomeInbox
                assert HomeInbox().pending() == []
            assert read_turn_marker(home, "agentready_home") is None
        finally:
            backend.should_exit = True
            thread.join(timeout=10)
            from tui_gateway.server import _shutdown_sessions
            _shutdown_sessions()
            for stopped, poller in gateway._notification_pollers:
                stopped.set()
                poller.join(timeout=2)
