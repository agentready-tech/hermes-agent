"""Explicitly admitted provider profile. Never auto-register on import."""

import logging
import re
from collections.abc import Callable
from subprocess import Popen
from threading import Event
from types import SimpleNamespace

from agent.redact import redact_for_egress
from hermes_runtime.standalone import agy_runtime
from providers.agy_policy import AgyError, LOCAL_ENDPOINT, MODEL, PROVIDER
from providers.agy_process import communicate
from providers.base import ProviderProfile

from .protocol import Stream, encode, prepare, validate_result

logger = logging.getLogger(__name__)
_PRIVATE_DIAGNOSTIC = re.compile(
    r"https?://[^\s\"'<>]+|(?:ya29\.|1//|4/0)[A-Za-z0-9._/~+-]+"
    r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
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

    def for_request(self):
        self.require_current()
        child = Client(self.home, self.authority, self.generation, self._cancelled)
        child.selection = self.selection
        return child

    def _cancelled(self):
        return self.closed.is_set() or self.cancelled()

    def create(self, *, model, messages, tools=None, stream=False, response_format=None, **kwargs):
        if model != MODEL or response_format is not None or kwargs.get("modalities") not in (None, ["text"]):
            raise AgyError("unsupported_input")
        prompt, validators = prepare(messages, tools)
        if self._cancelled():
            raise AgyError("cancelled")
        result = self._request(prompt, validators)
        self.require_current()
        return _completion(result, stream)

    def require_current(self):
        if self._cancelled():
            raise AgyError("cancelled")
        authority = agy_runtime.require_authority(self.authority, self.home, "inference")
        if authority.active(self.home) != self.generation:
            raise AgyError("auth_required")
        if self.selection is not None:
            self.selection.require_saved()
        return authority

    def _request(self, prompt, validators):
        from contextlib import nullcontext
        from hermes_cli.config import config_write_lock

        authority = self.authority
        try:
            with config_write_lock(self.home / "config.yaml") if self.selection is not None else nullcontext():
                self.require_current()
                child = authority.spawn(self.home, self.generation, "inference", agy_runtime.print_command(prompt))
            row = _read_response(child, cancelled=self._cancelled)
            return validate_result(row, validators)
        except AgyError as error:
            if error.code in {"auth_expired", "containment"}:
                authority.withdraw(self.home, self.generation, error.code)
            raise
        except OSError:
            raise AgyError("transport") from None


def _read_response(child: Popen[bytes], *, cancelled: Callable[[], bool]) -> dict[str, object]:
    parser = Stream()
    stderr = bytearray()
    try:
        status, _ = communicate(child, parser.feed, timeout=125, cancelled=cancelled, diagnostics=stderr)
        row = parser.finish(status)
        # A soft refusal may write only to stderr and still report SUCCESS.
        if stderr:
            raise AgyError("containment")
        return row
    except AgyError as error:
        if error.code != "cancelled":
            detail = parser.result.get("error") if parser.result is not None else None
            logger.warning(
                "Gemini Antigravity request failed (reason=%s, exit_code=%s, result.error=%r, stderr=%r)",
                error.code, child.returncode, _log_text(detail), _log_text(stderr.decode("utf-8", errors="replace")),
            )
        raise


def _log_text(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("message")
    if not isinstance(value, str):
        return ""
    return redact_for_egress(_PRIVATE_DIAGNOSTIC.sub("[redacted]", value)).strip()[:768]


def _completion(row, stream):
    calls = None
    if row["type"] == "tool_call":
        calls = [{"type": "function", "index": 0, "id": row["id"], "function": {
            "name": row["name"], "arguments": encode(row["arguments"]).decode("utf-8")}}]
    message = {"role": "assistant", "content": row.get("answer"), "tool_calls": calls}
    reason = "tool_calls" if calls else "stop"
    if stream:
        from openai.types.chat import ChatCompletionChunk
        chunk = ChatCompletionChunk(id="agy-step", created=0, object="chat.completion.chunk", model=MODEL,
                                    choices=[{"index": 0, "finish_reason": reason, "delta": message}])
        return iter((chunk,))
    from openai.types.chat import ChatCompletion
    return ChatCompletion(id="agy-step", created=0, object="chat.completion", model=MODEL,
                          choices=[{"index": 0, "finish_reason": reason, "message": message}])
