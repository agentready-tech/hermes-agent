"""Local configuration and durable event contracts for autonomous Hermes."""

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_runtime.standalone import config, events, gmail


@pytest.fixture
def home(monkeypatch):
    from hermes_runtime.adapters import hermes
    path = Path(os.environ["HERMES_HOME"]).resolve()
    path.mkdir(exist_ok=True)
    monkeypatch.setattr(hermes, "_home", None)
    config.activate_home(path)
    return path


def test_init_preserves_configuration_secrets_and_session(home):
    from hermes_runtime.adapters.state import current_home_session_id
    identity = current_home_session_id()
    (home / "config.yaml").write_text("# My model\nmodel: custom-model\nmemory:\n  provider: holographic\ncustom: keep\n")
    (home / ".env").write_text("OPENAI_API_KEY=private\nHERMES_DASHBOARD_SESSION_TOKEN=existing\n")
    (home / "workspace").mkdir(exist_ok=True)
    (home / "workspace" / "important.txt").write_text("keep")
    config.initialize(home)
    first = (home / ".env").read_text()
    config.initialize(home)
    assert config.read_config(home)["model"] == "custom-model"
    assert config.read_config(home)["custom"] == "keep"
    assert "# My model" in (home / "config.yaml").read_text()
    assert (home / ".env").read_text() == first
    assert "HERMES_DASHBOARD_SESSION_TOKEN=existing" in first
    assert (home / "workspace" / "important.txt").read_text() == "keep"
    assert current_home_session_id() == identity


def test_init_does_not_stamp_legacy_schema_or_switch_existing_memory(home):
    (home / "config.yaml").write_text("model: old-custom\nmemory:\n  memory_char_limit: 5000\n")
    config.initialize(home)
    stored = config.read_config(home)
    assert "_config_version" not in stored
    assert "provider" not in stored["memory"]
    assert stored["memory"]["memory_char_limit"] == 5000


def test_fresh_init_selects_holographic(tmp_path):
    home = tmp_path / "fresh-home"
    config.initialize(home)
    assert config.read_config(home)["memory"]["provider"] == "holographic"
    assert config.read_config(home)["database"]["journal_mode"] == "delete"


def test_host_event_uses_container_stdin_without_opening_live_database(home, tmp_path, monkeypatch):
    from hermes_runtime.standalone.cli import parser, execute
    from hermes_runtime.standalone.deployment import Docker
    from hermes_runtime.adapters.inbox import HomeInbox
    directory = tmp_path / "deployment"
    directory.mkdir()
    (directory / "deployment.json").write_text(json.dumps({"name": "owned-container", "identity": "test"}))
    calls = []
    monkeypatch.setattr(Docker, "owned", lambda *_: None)
    def call(self, *args, input=None):
        calls.append((args, input))
        return '{"message_id":"accepted-inside-container","pending":true}'
    monkeypatch.setattr(Docker, "call", call)
    content = "Literal $(data)\nSecond line"
    args = parser().parse_args(["--deployment", str(directory), "event", "--source", "mail", "--id", "one", content])
    assert execute(args, home)["message_id"] == "accepted-inside-container"
    assert calls[0][0][:3] == ("exec", "-i", "owned-container")
    assert calls[0][1] == content
    assert HomeInbox().pending() == []
    (directory / "transition.json").write_text("{}")
    with pytest.raises(RuntimeError, match="transition"):
        execute(args, home)


def test_mail_and_native_cron_retries_share_main_inbox(home):
    from cron.scheduler_authority import run_canonical_job
    from hermes_runtime.adapters.inbox import HomeInbox
    mail = events.enqueue("New delivery date", "mail", "id-42")
    assert events.enqueue("Repeated delivery", "mail", "id-42") == mail
    job = events.schedule("Review delivery", "every 1h", "Daily order")
    first = run_canonical_job(job, execution_id="fire-1")
    assert first[0] is True and "Accepted" in first[1]
    assert run_canonical_job(job, execution_id="fire-1") == first
    run_canonical_job(job, execution_id="fire-2")
    assert len(HomeInbox().pending()) == 3
    job["base_url"] = "http://example.test/v1"
    with pytest.raises(ValueError, match="explicit user request"):
        run_canonical_job(job, execution_id="fire-unsupported")
    assert len(HomeInbox().pending()) == 3
    job.pop("base_url")
    assert events.admit_cron({"id": "ordinary", "origin": None}) is None
    cancelled = threading.Event()
    cancelled.set()
    assert not run_canonical_job(job, execution_id="fire-cancelled", cancel_event=cancelled)[0]
    assert len(HomeInbox().pending()) == 3


@pytest.fixture
def fake_gmail():
    counts = {"messages": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.respond({"access_token": "test-token"})

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer test-token"
            if self.path == "/profile":
                self.respond({"emailAddress": "owner@example.test"})
            elif self.path.startswith("/messages?"):
                self.respond({"messages": [{"id": "mail-1"}]})
            else:
                counts["messages"] += 1
                self.respond({"id": "mail-1", "threadId": "thread-1", "payload": {
                    "headers": [{"name": "From", "value": "Supplier <supplier@example.test>"},
                                {"name": "Subject", "value": "Correction"}], "parts": [
                        {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Monday, not Friday").decode()}},
                        {"mimeType": "text/plain", "filename": "../../invoice.txt", "body": {"data": base64.urlsafe_b64encode(b"invoice").decode()}}]}})

        def respond(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield gmail.Gmail({"GMAIL_CLIENT_ID": "id", "GMAIL_CLIENT_SECRET": "secret", "GMAIL_REFRESH_TOKEN": "refresh"},
                          api=f"http://127.0.0.1:{server.server_port}", token_url=f"http://127.0.0.1:{server.server_port}/token"), counts
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_gmail_oauth_accept_before_retry_and_attachment_paths(home, fake_gmail, monkeypatch):
    from hermes_runtime.adapters.inbox import HomeInbox
    client, counts = fake_gmail
    original = gmail.enqueue
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("worker died after durable acceptance")
    monkeypatch.setattr(gmail, "enqueue", interrupted)
    settings = {"allowed_senders": ["supplier@example.test"]}
    with pytest.raises(RuntimeError):
        client.poll(home, settings)
    assert len(HomeInbox().pending()) == 1
    assert client.poll(home, settings) == 0
    assert counts["messages"] == 1
    pending = HomeInbox().pending()[0][1]
    assert "Monday, not Friday" in pending
    attachment = home / "workspace" / "mail" / "mail-1" / "0-invoice.txt"
    assert attachment.read_text() == "invoice"
    with pytest.raises(ValueError, match="allowed_senders"):
        client.poll(home, {})


def test_gmail_denied_sender_never_enters_inbox(home, fake_gmail):
    from hermes_runtime.adapters.inbox import HomeInbox
    client, _ = fake_gmail
    assert client.poll(home, {"allowed_senders": ["other@example.test"]}) == 0
    assert HomeInbox().pending() == []
