"""Explicitly admitted provider profile. Never auto-register on import."""

from threading import Event
from types import SimpleNamespace

from hermes_runtime.standalone.agy_runtime import print_command
from providers.agy_policy import AgyError, LOCAL_ENDPOINT, MODEL, PROVIDER
from providers.agy_process import communicate
from providers.base import ProviderProfile

from .protocol import Stream, encode, prepare, validate_result


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

    def close(self):
        self.closed.set()

    def _cancelled(self):
        return self.closed.is_set() or self.cancelled()

    def create(self, *, model, messages, tools=None, stream=False, response_format=None, **kwargs):
        if model != MODEL or response_format is not None or kwargs.get("modalities") not in (None, ["text"]):
            raise AgyError("unsupported_input")
        prompt, validators = prepare(messages, tools)
        if self._cancelled():
            raise AgyError("cancelled")
        result = self._request(prompt, validators)
        if self._cancelled():
            raise AgyError("cancelled")
        return _completion(result, stream)

    def _request(self, prompt, validators):
        from hermes_runtime.standalone import agy_runtime
        authority = agy_runtime.admit(self.home, "inference")
        if authority is not self.authority or authority.active(self.home) != self.generation:
            raise AgyError("auth_required")
        parser = Stream()
        try:
            child = authority.spawn(self.home, self.generation, "inference", print_command(prompt))
            rc, diagnostics = communicate(child, parser.feed, timeout=125, cancelled=self._cancelled)
            row = parser.finish(rc)
            if diagnostics:
                # Nothing on this channel is a qualified auth or success signal.
                # A soft refusal may come with SUCCESS; refuse publication either way.
                raise AgyError("containment")
            return validate_result(row, validators)
        except AgyError as error:
            if error.code in {"auth_expired", "containment"}:
                authority.withdraw(self.home, self.generation, error.code)
            raise
        except OSError:
            raise AgyError("transport") from None


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
