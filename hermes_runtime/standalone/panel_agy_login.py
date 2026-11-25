"""Protected owner-bound Antigravity login. No credential exchange in Hermes."""

from dataclasses import dataclass
import re
import secrets
from threading import Event, RLock
from urllib.parse import parse_qs, urlsplit

from agent.memory_provider import spawn_context_thread
from hermes_runtime.standalone import agy_runtime
from providers.agy_policy import AgyError, CAPABILITY, MODEL
from providers.agy_process import communicate

CODE = re.compile(r"[!-~]{1,4096}")
VIEWER = re.compile(r"[A-Za-z0-9_-]{43}")


def login_url(value):
    if len(value.encode("utf-8")) > 4096 or not CODE.fullmatch(value):
        return False
    try:
        url = urlsplit(value)
        fields = parse_qs(url.query, max_num_fields=32, strict_parsing=True)
    except ValueError:
        return False
    return (url.scheme == "https" and url.netloc == "accounts.google.com"
            and url.path == "/o/oauth2/v2/auth" and "#" not in value
            and all(len(v) == 1 for v in fields.values())
            and fields.get("redirect_uri") == ["https://codeassist.google.com/authcode"]
            and fields.get("response_type") == ["code"] and bool(fields.get("state"))
            and bool(fields.get("code_challenge")) and fields.get("code_challenge_method") == ["S256"])


@dataclass(frozen=True)
class Readback:
    generation: str
    namespace: str
    policy: str
    subject: str
    pid: int


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
        self.child = authority.spawn(home, stage.generation, "login", agy_runtime.LOGIN_COMMAND)
        self.worker = spawn_context_thread(self._run, name="protected-agy-login")
        self.worker.start()

    def _run(self):
        buffer = bytearray()
        try:
            rc, diagnostics = communicate(self.child, lambda data: self._collect(buffer, data), timeout=self.timeout,
                                           cancelled=self.cancelled.is_set, input_data=self._take_code)
            if rc or buffer or not self.submitted or not self.url:
                status = "error_invalid_code" if self.submitted and b"invalid_grant" in diagnostics else "error"
                self._finish(status)
                return
            self._finish("verifying")
            proof = self.authority.verify(self.home, self.stage, self.cancelled.is_set)
            if self._matches(proof):
                with self.lock:
                    if (not self.cancelled.is_set() and agy_runtime.admit(self.home, "login") is self.authority
                            and self.authority.promote(self.home, self.stage)):
                        self.status = "approved"
                        return
            self._finish("error")
        except (AgyError, OSError, ValueError):
            self._finish("cancelled" if self.cancelled.is_set() else "error")
        finally:
            try:
                if self.status != "approved":
                    self.authority.discard(self.home, self.stage)
            finally:
                self._finish(self.status if self.status not in {"pending", "verifying"} else "error")
                self.done.set()

    def _matches(self, proof):
        return (isinstance(proof, Readback) and proof.pid > 0 and proof.pid != self.child.pid
                and (proof.generation, proof.namespace, proof.policy, proof.subject) == (
                    self.stage.generation, self.stage.namespace, self.stage.policy, self.stage.subject))

    def _finish(self, status):
        with self.lock:
            self.status, self.url, self.code = status, None, None

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

    def handle(self, method, params, viewer):
        if not isinstance(viewer, str) or not VIEWER.fullmatch(viewer):
            raise AgyError("unavailable")
        if method == "subscriptions.models":
            self.connected()
            return {"models": [{"id": MODEL, "label": MODEL}]}
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
