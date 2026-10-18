"""Real CLI/backend/agent loops exchange mail before the delegated task ends."""

import http.server
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from tests.gateway.fixtures.local_recovery_probe import daemon


def test_cli_parent_answers_running_child_in_the_same_sessions(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    errors = []

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(404)

        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = request.get("messages", [])
                text = json.dumps(messages)
                child = any(m["role"] == "user" and "WORKER_MAIL" in str(m.get("content")) for m in messages)
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                args, answer = None, None
                if child:
                    if "QUESTION_FROM_CHILD" not in text:
                        args = {"action": "message", "subagent_id": "parent", "message": "QUESTION_FROM_CHILD"}
                    elif "ANSWER_TO_CHILD" in text:
                        answer = "CHILD_FINISHED_AFTER_REPLY"
                    else:
                        args = {"action": "wait", "timeout_seconds": 1}
                elif not results:
                    args = {"tasks": [{"goal": "WORKER_MAIL: ask your parent for clarification and continue after its reply"}]}
                elif "QUESTION_FROM_CHILD" in text and "ANSWER_TO_CHILD" not in text:
                    target = next(r["projects"][0]["subagent_id"] for r in results if r.get("projects"))
                    args = {"action": "message", "subagent_id": target, "message": "ANSWER_TO_CHILD"}
                elif "CHILD_FINISHED_AFTER_REPLY" in text:
                    answer = "MAIL_ROUNDTRIP_OK"
                elif "finished input" in text:
                    target = next(r["projects"][0]["subagent_id"] for r in results if r.get("projects"))
                    args = {"action": "read", "subagent_id": target}
                else:
                    args = {"action": "wait", "timeout_seconds": 1}
                message = {"role": "assistant", "content": answer}
                if args:
                    message["tool_calls"] = [{"id": "call_" + str(len(messages)), "type": "function",
                        "function": {"name": "delegate_task", "arguments": json.dumps(args)}}]
                response = {"id": "chatcmpl-mail", "object": "chat.completion", "created": 1, "model": "test-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if args else "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
                content_type = "application/json"
                if request.get("stream"):
                    response["object"] = "chat.completion.chunk"
                    response["choices"][0]["delta"] = response["choices"][0].pop("message")
                    for index, tool in enumerate(message.get("tool_calls", [])):
                        tool["index"] = index
                    raw = ("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as error:
                errors.append(repr(error))
                self.send_error(500)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    (home / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {url}\n  api_mode: chat_completions\n"
        "compression:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n"
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
        "gateway:\n  multiplex_profiles: false\nterminal:\n  env: local\n"
        f"delegation:\n  base_url: {url}\n  api_key: test-only\n  model: test-model\n"
        "  worktree_isolation: false\n  max_spawn_depth: 1\n  max_iterations: 20\n"
    )
    repo = str(Path(__file__).resolve().parents[2])
    query = tmp_path / "query.txt"
    query.write_text("Delegate a task, answer its clarification and use the completed result.")
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA") if key in os.environ}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(home), TERMINAL_CWD=str(tmp_path),
        HERMES_MANAGED_DIR=str(tmp_path / "managed"), OPENAI_BASE_URL=url, OPENAI_API_KEY="test-only", PYTHONPATH=repo,
        PYTHONDONTWRITEBYTECODE="1", LANG="C.UTF-8")
    command = [sys.executable, "-c", "from hermes_cli.main import main; main()", "chat", "-Q", "--provider", "custom",
        "--model", "test-model", "--base-url", url, "--toolsets", "delegation", "--ignore-rules", "--query-file", str(query),
        "--reasoning", "high", "--max-turns", "20"]
    try:
        with daemon(repo, home, env, barrier=False):
            result = subprocess.run(command, cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=75, check=False)
        assert result.returncode == 0 and "MAIL_ROUNDTRIP_OK" in result.stdout, (result.stdout, result.stderr, errors)
        assert not errors
        with sqlite3.connect(home / "state.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 1
            assert conn.execute("SELECT state FROM delegated_project_messages").fetchone()[0] == "completed"
            assert conn.execute("SELECT COUNT(*) FROM delegation_mail WHERE kind='message' AND delivered_at IS NOT NULL").fetchone()[0] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
