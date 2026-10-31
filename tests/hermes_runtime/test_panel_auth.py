"""The Panel connector follows the dashboard's gated auth, without accepting its legacy token."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_runtime.standalone.panel_auth import NativeScreenAuth


@pytest.fixture
def dashboard():
    state = {"session": False, "logins": 0, "tickets": 0, "reject_login": False, "loopback": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, value, *, cookie=False):
            self.send_response(status)
            if cookie:
                self.send_header("Set-Cookie", "session=verified; HttpOnly; Path=/")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def authenticated(self):
            return state["session"] and self.headers.get("Cookie") == "session=verified"

        def do_GET(self):
            if state["loopback"]:
                return self.reply(200, {"provider": "loopback"})
            return self.reply(200, {"provider": "basic"}) if self.authenticated() else self.reply(401, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/auth/password-login":
                state["logins"] += 1
                if state["reject_login"] or body != {"provider": "basic", "username": "owner", "password": "private"}:
                    return self.reply(401, {})
                state["session"] = True
                return self.reply(200, {"ok": True}, cookie=True)
            if self.path == "/api/auth/ws-ticket" and self.authenticated():
                state["tickets"] += 1
                return self.reply(200, {"ticket": f"single-use-{state['tickets']}"})
            return self.reply(401, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gated_dashboard_reuses_cookie_and_renews_expired_session(dashboard):
    port, state = dashboard
    auth = NativeScreenAuth(port, "rejected-legacy-token", username="owner", password="private")
    first, second = auth.rpc_url(), auth.rpc_url()
    assert parse_qs(urlsplit(first).query) == {"ticket": ["single-use-1"]}
    assert parse_qs(urlsplit(second).query) == {"ticket": ["single-use-2"]}
    assert state["logins"] == 1
    state["session"] = False
    assert parse_qs(urlsplit(auth.rpc_url()).query) == {"ticket": ["single-use-3"]}
    assert state["logins"] == 2


def test_invalid_password_does_not_fall_back_to_legacy_token(dashboard):
    port, state = dashboard
    state["reject_login"] = True
    auth = NativeScreenAuth(port, "legacy", username="owner", password="wrong")
    with pytest.raises(HTTPError) as error:
        auth.rpc_url()
    assert error.value.code == 401
    assert state["tickets"] == 0


def test_loopback_dashboard_retains_its_native_token(dashboard):
    port, state = dashboard
    state["loopback"] = True
    auth = NativeScreenAuth(port, "local-token", username="owner", password="private")
    assert parse_qs(urlsplit(auth.rpc_url()).query) == {"token": ["local-token"]}
    assert state["logins"] == state["tickets"] == 0
