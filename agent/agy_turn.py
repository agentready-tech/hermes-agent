"""Guard the existing native dispatch, never execute from the provider adapter."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock

from agent.message_sanitization import coalesce_tool_call_id
from providers.agy_policy import AgyError


class AcpScope:
    """The request thread shares this handle with its owning conversation turn."""

    def __init__(self):
        self.lock = Lock()
        self.cancelled = Event()
        self.session = None

    def close_if_idle(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.session is not None:
                session, self.session = self.session, None
                session.close()
        finally:
            self.lock.release()


acp_scope: ContextVar[AcpScope | None] = ContextVar("agy_acp_scope", default=None)


@contextmanager
def acp_turn_scope():
    scope = AcpScope()
    token = acp_scope.set(scope)
    try:
        yield
    finally:
        acp_scope.reset(token)
        scope.cancelled.set()
        scope.close_if_idle()


def _history_ids(rows):
    for row in rows:
        if row.get("role") == "assistant":
            yield from (coalesce_tool_call_id(call) for call in row.get("tool_calls") or ())
        elif row.get("role") == "tool":
            yield row.get("tool_call_id")


def reject_proposal(agent, calls, messages):
    if agent._interrupt_requested:
        return AgyError("cancelled")
    if len(calls) != 1 or calls[0].function.name not in agent.valid_tool_names:
        return AgyError("protocol")
    call_id = coalesce_tool_call_id(calls[0])
    if not call_id or call_id != calls[0].id:
        return AgyError("protocol")
    admitted = agent._agy_admitted_call_ids
    if call_id in admitted or call_id in _history_ids(messages):
        return AgyError("duplicate_tool_call")
    db = agent._session_db
    if db is not None:
        rows = db.get_messages_as_conversation(agent.session_id, include_ancestors=True, include_compacted=True)
        if call_id in _history_ids(rows):
            return AgyError("duplicate_tool_call")
    admitted.add(call_id)
    return None


def initialize(agent, base_url):
    from providers.agy_policy import LOCAL_ENDPOINT, PROVIDER, is_agy
    if not is_agy(agent.provider):
        return False
    if agent.provider != PROVIDER or (base_url and base_url != LOCAL_ENDPOINT):
        raise AgyError("unavailable")
    agent.api_mode, agent.base_url, agent.api_key = "chat_completions", LOCAL_ENDPOINT, "local-process"
    agent._client_kwargs = {"base_url": LOCAL_ENDPOINT, "api_key": agent.api_key}
    agent.client = agent._create_openai_client(agent._client_kwargs, reason="agent_init", shared=True)
    return True


def supplied_client(agent):
    from hermes_constants import get_hermes_home
    from hermes_runtime.standalone.agy_runtime import profile_for
    from providers.agy_policy import PROVIDER, is_agy
    provider = getattr(agent, "provider", None)
    if not is_agy(provider):
        return None
    if provider != PROVIDER:
        raise AgyError("unavailable")
    return profile_for(get_hermes_home()).create_client(cancelled=lambda: bool(agent._interrupt_requested))


def reject_in_round(agent, calls, messages, guarded):
    return reject_proposal(agent, calls, messages) if guarded else None


def refused_flush(outcome, guarded):
    return outcome is not True if guarded else outcome is False


def require_current_tool_authority(agent):
    from providers.agy_policy import is_agy
    if not is_agy(getattr(agent, "provider", None)):
        return
    if agent._interrupt_requested:
        raise AgyError("cancelled")
    agent.client.require_current()
