"""Authenticate the screen connector through the native dashboard's local login API."""

import json
import threading
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


class NativeScreenAuth:
    """One profile worker's cookie session; each viewer gets a fresh, single-use WS ticket."""

    def __init__(self, port, legacy_token, *, username=None, password=None):
        self.origin = f"http://127.0.0.1:{int(port)}"
        self.websocket = f"ws://127.0.0.1:{int(port)}/api/ws"
        self.legacy_token, self.username, self.password = legacy_token, username, password
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.lock = threading.Lock()

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.origin + path, data=data,
                          headers={"Content-Type": "application/json", "Origin": self.origin})
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)

    def rpc_url(self, events=False):
        target = self.websocket.replace("/api/ws", "/api/events") if events else self.websocket
        channel = {"channel": "agentready_home"} if events else {}
        # Older loopback-only installations have no password provider. Their existing
        # process token remains valid; a gated dashboard still rejects it itself.
        if not self.username or not self.password:
            return target + "?" + urlencode({"token": self.legacy_token, **channel})
        with self.lock:
            try:
                identity = self.request("/api/auth/me")
            except HTTPError as error:
                if error.code != 401:
                    raise
                self.request("/auth/password-login", {
                    "provider": "basic", "username": self.username, "password": self.password,
                })
            else:
                if identity.get("provider") == "loopback":
                    return target + "?" + urlencode({"token": self.legacy_token, **channel})
            ticket = self.request("/api/auth/ws-ticket", {})["ticket"]
            return target + "?" + urlencode({"ticket": ticket, **channel})
