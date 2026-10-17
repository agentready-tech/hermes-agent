"""Real native processes consume events without a human client and survive a restart."""

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.waiting import wait_until


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(body)
        if getattr(self.server, "block_next", False) and "Delivery Friday" in json.dumps(body):
            self.server.block_next = False
            self.server.entered.set()
            self.server.release.wait(timeout=15)
        common = {"id": "autonomous-test", "created": 1, "model": "gpt-4.1"}
        text = "Delivery reviewed in the same main session."
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "content": text}, None), ({}, "stop")]:
                event = {**common, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({**common, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}).encode())


@pytest.mark.platforms("posix")
def test_native_service_late_event_child_restart_and_boot_recovery():
    from hermes_runtime.standalone.supervisor import status
    from tui_gateway.turn_marker import read_turn_marker
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.calls = []
    provider.block_next = True
    provider.entered, provider.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="ha-") as temporary:
        home = Path(temporary).resolve()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        (home / "config.yaml").write_text("model:\n  provider: custom\n  default: gpt-4.1\n"
            f"  base_url: http://127.0.0.1:{provider.server_port}/v1\ncompression:\n  enabled: false\n"
            "memory:\n  provider: holographic\n")
        (home / ".env").write_text("OPENAI_API_KEY=test-only\n")
        command = [sys.executable, "-m", "hermes_runtime.standalone.cli", "--home", str(home)]
        environment = {**os.environ, "HERMES_HOME": str(home)}

        def cli(*args):
            result = subprocess.run([*command, *args], env=environment, capture_output=True, text=True, timeout=20, check=False)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        def ready():
            return status(home).get("ok")

        def responses():
            if not (home / "state.db").exists():
                return 0
            with sqlite3.connect(home / "state.db") as db:
                return db.execute("SELECT COUNT(*) FROM messages WHERE session_id='agentready_home' AND role='assistant' AND content=?",
                                  ("Delivery reviewed in the same main session.",)).fetchone()[0]

        def boot(log):
            child = subprocess.Popen([*command, "run", "--host", "0.0.0.0", "--port", str(port)], env=environment,
                                     stdout=log, stderr=log, start_new_session=True)
            wait_until(lambda: ready() or child.poll() is not None, timeout=40, what="native service readiness")
            assert child.poll() is None, (home / "test-service.log").read_text()[-5000:]
            return child

        def stop(child):
            child.terminate()
            try:
                child.wait(timeout=55)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

        child = None
        try:
            cli("init")
            with (home / "test-service.log").open("a") as log:
                child = boot(log)
                cli("event", "--source", "mail", "--id", "one", "Delivery Friday")
                wait_until(provider.entered.is_set, timeout=25, what="active native main turn")
                cli("event", "--source", "mail", "--id", "during-turn", "Supplier invoice arrived")
                provider.release.set()
                wait_until(lambda: responses() >= 1 and read_turn_marker(home, "agentready_home") is None,
                           timeout=35, what="completed late inbox event without human socket")
                assert any("Supplier invoice arrived" in json.dumps(call) for call in provider.calls)
                first_responses = responses()
                initial = status(home)
                os.kill(initial["children"]["backend"]["pid"], signal.SIGKILL)
                wait_until(lambda: ready() and status(home)["children"]["backend"]["pid"] != initial["children"]["backend"]["pid"],
                           timeout=40, what="native backend replacement")
                assert status(home)["children"]["gateway"]["pid"] == initial["children"]["gateway"]["pid"]
                stop(child)
                child = None
                cli("event", "--source", "mail", "--id", "two", "Correction: delivery Monday")
                child = boot(log)
                wait_until(lambda: responses() > first_responses and read_turn_marker(home, "agentready_home") is None
                    and any("Monday" in json.dumps(call) for call in provider.calls), timeout=35, what="durable event after restart")
                assert any("Friday" in json.dumps(call) and "Monday" in json.dumps(call) for call in provider.calls), provider.calls
                with sqlite3.connect(home / "state.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM agentready_inbox WHERE delivered_at IS NULL").fetchone()[0] == 0
                    assert db.execute("SELECT current_session_id FROM agentready_session_heads").fetchone()[0] == "agentready_home"
                    for text in ("Delivery Friday", "Supplier invoice arrived", "Correction: delivery Monday"):
                        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND content LIKE ?", ("%" + text + "%",)).fetchone()[0] == 1
        except AssertionError as error:
            with sqlite3.connect(home / "state.db") as db:
                messages = db.execute("SELECT session_id,role,substr(content,1,500) FROM messages ORDER BY id").fetchall()
            calls = [[(m.get("role"), str(m.get("content"))[:500]) for m in c.get("messages", []) if m.get("role") != "system"] for c in provider.calls]
            raise AssertionError({"error": str(error), "calls": calls, "messages": messages,
                                  "marker": read_turn_marker(home, "agentready_home"),
                                  "log": (home / "test-service.log").read_text()[-3500:]}) from error
        finally:
            if child is not None:
                stop(child)
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)
