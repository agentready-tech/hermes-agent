"""Protected owner-bound Antigravity login. No credential exchange in Hermes."""

import logging
import re
import secrets
from threading import Event, RLock
from urllib.parse import parse_qs, urlsplit

from agent.memory_provider import spawn_context_thread
from hermes_runtime.standalone import agy_runtime
from providers.agy_policy import AgyError, CAPABILITY
from providers.agy_process import communicate

logger = logging.getLogger(__name__)
CODE = re.compile(r"[!-~]{1,4096}")
VIEWER = re.compile(r"[A-Za-z0-9_-]{43}")
LOGIN_CALLBACKS = {
    "/o/oauth2/auth": "https://antigravity.google/oauth-callback",
    "/o/oauth2/v2/auth": "https://codeassist.google.com/authcode",
}


def login_url(value):
    if len(value.encode("utf-8")) > 4096 or not CODE.fullmatch(value):
        return False
    try:
        url = urlsplit(value)
        fields = parse_qs(url.query, max_num_fields=32, strict_parsing=True)
    except ValueError:
        return False
    callback = LOGIN_CALLBACKS.get(url.path)
    return (url.scheme == "https" and url.netloc == "accounts.google.com"
            and callback is not None and "#" not in value
            and all(len(v) == 1 for v in fields.values())
            and fields.get("redirect_uri") == [callback]
            and fields.get("response_type") == ["code"] and bool(fields.get("state"))
            and bool(fields.get("code_challenge")) and fields.get("code_challenge_method") == ["S256"])


class Attempt:
    def __init__(self, authority, home, stage, viewer, *, validate_url=login_url, timeout=900):
        self.id = secrets.token_urlsafe(24)
        self.viewer, self.stage = viewer, stage
        self.status, self.url, self.code = "pending", None, None
        self.submitted = False
        self.bytes = 0
        self.lock, self.done, self.cancelled = RLock(), Event(), Event()
        self.home, self.authority, self.validate_url = home, authority, validate_url
        self.timeout = timeout
        self.child = agy_runtime.start_login(authority, home, stage)
        self.worker = spawn_context_thread(self._run, name="protected-agy-login")
        self.worker.start()

    def _run(self):
        buffer = bytearray()
        phase = "exchange"
        try:
            rc, diagnostics = communicate(self.child, lambda data: self._collect(buffer, data), timeout=self.timeout,
                                           cancelled=self.cancelled.is_set, input_data=self._take_code)
            if rc or buffer or not self.submitted or not self.url:
                status = "error_invalid_code" if self.submitted and b"invalid_grant" in diagnostics else "error"
                self._fail(phase, "invalid_grant" if status == "error_invalid_code" else "invalid_result", status)
                return
            phase = "probe"
            self._finish("verifying")
            agy_runtime.verify_staged(self.authority, self.home, self.stage, self.child.pid, self.cancelled.is_set)
            phase = "activate"
            with self.lock:
                if self.cancelled.is_set():
                    raise AgyError("cancelled")
                agy_runtime.require_authority(self.authority, self.home, "login")
                if self.authority.promote(self.home, self.stage):
                    self.status = "approved"
                    return
            self._fail(phase, "stale_binding")
        except (OSError, ValueError) as error:
            if self.cancelled.is_set():
                self._finish("cancelled")
            else:
                self._fail(phase, error.code if isinstance(error, AgyError) else "internal")
        finally:
            try:
                if self.status != "approved":
                    self.authority.discard(self.home, self.stage)
            finally:
                self._finish(self.status if self.status not in {"pending", "verifying"} else "error")
                self.done.set()

    def _finish(self, status):
        with self.lock:
            self.status, self.url, self.code = status, None, None

    def _fail(self, phase, reason, status="error"):
        # Child output can contain account information and OAuth material.
        logger.warning("Gemini sign-in failed (phase=%s, reason=%s)", phase, reason)
        self._finish(status)

    def _collect(self, buffer, data):
        self.bytes += len(data)
        buffer.extend(data)
        if self.bytes > 65536 or len(buffer) > 16384:
            raise AgyError("transport")
        while (end := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:end]).rstrip(b"\r")
            del buffer[:end + 1]
            for value in line.decode("utf-8").split():
                if value.startswith("https://"):
                    with self.lock:
                        if self.url or not self.validate_url(value):
                            raise AgyError("protocol")
                        self.url = value

    def _take_code(self):
        with self.lock:
            value, self.code = self.code, None
            if value is not None:
                if self.cancelled.is_set():
                    raise AgyError("cancelled")
                agy_runtime.require_authority(self.authority, self.home, "login")
            return value

    def submit(self, code):
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise AgyError("protocol")
        with self.lock:
            if self.submitted or self.status != "pending" or not self.url or self.cancelled.is_set():
                raise AgyError("protocol")
            self.code = code.encode("ascii") + b"\n"
            self.submitted = True

    def view(self):
        with self.lock:
            view = {"sessionId": self.id, "status": self.status, "flow": "authorization_code",
                    "expiresIn": self.timeout, "pollInterval": 5, "submitted": self.submitted}
            if self.url and not self.submitted:
                view["url"] = self.url
            return view

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            self.url = self.code = None
        if self.worker is not None:
            self.worker.join(timeout=3)


class GeminiSubscriptions:
    def __init__(self, home, *, attempt_factory=Attempt):
        self.home, self.attempt_factory = home, attempt_factory
        self.attempt = None
        self.lock = RLock()

    def card(self):
        try:
            authority = agy_runtime.admit(self.home, "catalog")
        except AgyError:
            return None
        return {"id": "gemini", "capability": CAPABILITY, "available": True,
                "connected": authority.active(self.home) is not None}

    def retire(self):
        with self.lock:
            if self.attempt is not None:
                self.attempt.cancel()
                self.attempt = None

    def connected(self):
        authority = agy_runtime.admit(self.home, "catalog")
        if authority.active(self.home) is None:
            raise AgyError("auth_required")
        return authority

    def model_catalog(self):
        authority = self.connected()
        generation = authority.active(self.home)
        if generation is None:
            raise AgyError("auth_required")
        catalog = authority.models(self.home, generation)
        return authority, generation, catalog

    def handle(self, method, params, viewer):
        if not isinstance(viewer, str) or not VIEWER.fullmatch(viewer):
            raise AgyError("unavailable")
        if method == "subscriptions.models":
            _, _, catalog = self.model_catalog()
            return {"models": [{"id": name, "label": label} for name, label in catalog]}
        authority = agy_runtime.admit(self.home, "login")
        with self.lock:
            if method == "subscriptions.login.start":
                if self.attempt is not None and not self.attempt.done.is_set():
                    raise AgyError("unavailable")
                stage = authority.stage(self.home)
                try:
                    self.attempt = self.attempt_factory(authority, self.home, stage, viewer)
                except (AgyError, OSError):
                    authority.discard(self.home, stage)
                    raise
                return self.attempt.view()
            attempt = self.attempt
            if not attempt or attempt.authority is not authority or (attempt.viewer, attempt.id) != (viewer, params.get("sessionId")):
                raise AgyError("unavailable")
            if method == "subscriptions.login.poll":
                return attempt.view()
            if method == "subscriptions.login.cancel":
                self.retire()
                return {"status": "cancelled"}
            if method == "subscriptions.login.code":
                attempt.submit(params.get("code"))
                return {"submitted": True}
        raise AgyError("unavailable")
