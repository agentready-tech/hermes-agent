"""Explicitly admitted provider profile. Never auto-register on import."""

import logging
import re
import time
from contextlib import nullcontext, suppress
from subprocess import SubprocessError
from threading import Event
from types import SimpleNamespace

from agent.agy_turn import acp_scope
from agent.redact import redact_for_egress
from hermes_runtime.standalone import agy_runtime
from providers.agy_acp_wire import Peer, encode as encode_wire
from providers.agy_policy import AgyError, LOCAL_ENDPOINT, PROMPT_LIMIT, PROVIDER, require_model
from providers.base import ProviderProfile

from .protocol import _message, _tool_catalog, encode, pack_prompt, prepare, validate_result

logger = logging.getLogger(__name__)
_PRIVATE_DIAGNOSTIC = re.compile(
    r"https?://[^\s\"'<>]+|(?:ya29\.|1//|4/0)[A-Za-z0-9._/~+-]+"
    r"|(?<![\w.+-])[\w.+-]{1,64}@[\w.-]{1,253}\.[A-Za-z]{2,63}",
    re.IGNORECASE,
)


class AgyProfile(ProviderProfile):
    def __init__(self, home, authority, generation):
        super().__init__(name=PROVIDER, base_url=LOCAL_ENDPOINT, auth_type="external_process", hidden=True,
                         supports_health_check=False, supports_model_listing=False)
        self.home, self.authority, self.generation = home, authority, generation

    def create_client(self, *, cancelled, **_ignored):
        return Client(self.home, self.authority, self.generation, cancelled)


class Client:
    base_url = LOCAL_ENDPOINT

    def __init__(self, home, authority, generation, cancelled):
        self.home, self.authority, self.generation = home, authority, generation
        self.cancelled, self.closed = cancelled, Event()
        self.chat = SimpleNamespace(completions=self)
        self.selection = None

    def close(self):
        self.closed.set()

    cancel = close

    def for_request(self, *, cancelled=None):
        self.require_current()
        # A summary follows its compaction fence instead of the chat turn's soft interrupt.
        cancel_check = self._cancelled if cancelled is None else lambda: self.closed.is_set() or cancelled()
        child = Client(self.home, self.authority, self.generation, cancel_check)
        child.selection = self.selection
        return child

    def preflight_input_size(self, *, model, messages, tools):
        scope = acp_scope.get()
        if scope is None or scope.session is not None:
            return None  # An open session receives only the pending tool result.
        try:
            packed, _ = pack_prompt(messages, tools, model=model)
        except AgyError:
            return None  # The normal request path reports unsupported input.
        return len(packed), PROMPT_LIMIT

    def _cancelled(self):
        return self.closed.is_set() or self.cancelled()

    def create(self, *, model, messages, tools=None, stream=False, response_format=None, **kwargs):
        require_model(model)
        if response_format is not None or kwargs.get("modalities") not in (None, ["text"]):
            raise AgyError("unsupported_input")
        if self._cancelled():
            raise AgyError("cancelled")
        result = self._request_acp(model, messages, tools)
        self.require_current()
        return _completion(result, model, stream)

    def require_current(self):
        if self._cancelled():
            raise AgyError("cancelled")
        authority = agy_runtime.require_authority(self.authority, self.home, "inference")
        if authority.active(self.home) != self.generation:
            raise AgyError("auth_required")
        if self.selection is not None:
            self.selection.require_saved()
        return authority

    def _request_acp(self, model, messages, tools):
        from hermes_cli.config import config_write_lock

        scope = acp_scope.get()
        if scope is None or not scope.lock.acquire(blocking=False):
            raise AgyError("unavailable")
        cancelled = lambda: self._cancelled() or scope.cancelled.is_set()
        owner = (self.home, self.authority, self.generation, self.selection, model)
        try:
            if cancelled():
                raise AgyError("cancelled")
            session = scope.session
            if session is None:
                prompt, validators = prepare(messages, tools, model=model)
                with config_write_lock(self.home / "config.yaml") if self.selection is not None else nullcontext():
                    self.require_current()
                    child = self.authority.spawn(self.home, self.generation, "inference", prompt)
                session = scope.session = AcpTurn(owner, child)
            else:
                if session.owner != owner:
                    raise AgyError("unavailable")
                self.require_current()
                _, validators = _tool_catalog(tools)
                session.resume(messages)
            row = session.receive(cancelled, validators)
            if row["type"] == "final":
                session.close()
                scope.session = None
            return row
        except (AgyError, OSError) as error:
            if scope.session is not None:
                scope.session.close()
                scope.session = None
            if isinstance(error, OSError):
                raise AgyError("transport") from None
            if error.code in {"auth_expired", "containment"}:
                self.authority.withdraw(self.home, self.generation, error.code)
            raise
        finally:
            scope.lock.release()
            if scope.cancelled.is_set():
                scope.close_if_idle()


class AcpTurn:
    """A native MCP request is answered by the next canonical Hermes tool result."""

    def __init__(self, owner, child):
        self.owner = owner
        self.peer = Peer(child)
        self.peer.send(child.agy_request_frame)
        self.pending = None

    def close(self):
        with suppress(OSError, SubprocessError):
            self.peer.close()

    def resume(self, messages):
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") == "tool" and message.get("tool_call_id") == self.pending:
                try:
                    result = _message(message)
                    after = [_message(row) for row in messages[index + 1:]]
                except (KeyError, ValueError, TypeError):
                    raise AgyError("unsupported_input") from None
                if any(row["role"] not in {"system", "developer", "user"} for row in after):
                    raise AgyError("protocol")
                self.peer.send(encode_wire({"type": "tool_result", "id": self.pending,
                                            "content": result["content"], "after": after}))
                self.pending = None
                return
        raise AgyError("protocol")

    def receive(self, cancelled, validators):
        deadline = time.monotonic() + 120
        row = {}
        try:
            row = self.peer.receive(lambda: deadline, cancelled)
            if row.get("type") == "error":
                reason = row.get("reason")
                if reason not in ("transport", "protocol", "auth_expired", "containment", "unsupported_model"):
                    reason = "protocol"
                raise AgyError(reason)
            result = validate_result(row, validators)
            if result["type"] == "tool_call":
                self.pending = result["id"]
            return result
        except AgyError as error:
            if error.code != "cancelled":
                logger.warning(
                    "Gemini Antigravity ACP failed (stage=%r, reason=%s, detail=%r, stderr=%r, exec_stderr=%r)",
                    _log_text(row.get("stage")), error.code, _log_text(row.get("detail")), _log_text(row.get("stderr")),
                    _log_text(self.peer.stderr.decode(errors="replace")),
                )
            raise


def _log_text(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("message")
    if not isinstance(value, str):
        return ""
    return redact_for_egress(_PRIVATE_DIAGNOSTIC.sub("[redacted]", value)).strip()[:768]


def _completion(row, model, stream):
    calls = None
    if row["type"] == "tool_call":
        calls = [{"type": "function", "index": 0, "id": row["id"], "function": {
            "name": row["name"], "arguments": encode(row["arguments"]).decode("utf-8")}}]
    message = {"role": "assistant", "content": row.get("answer"), "tool_calls": calls}
    reason = "tool_calls" if calls else "stop"
    if stream:
        from openai.types.chat import ChatCompletionChunk
        chunk = ChatCompletionChunk(id="agy-step", created=0, object="chat.completion.chunk", model=model,
                                    choices=[{"index": 0, "finish_reason": reason, "delta": message}])
        return iter((chunk,))
    from openai.types.chat import ChatCompletion
    return ChatCompletion(id="agy-step", created=0, object="chat.completion", model=model,
                          choices=[{"index": 0, "finish_reason": reason, "message": message}])
