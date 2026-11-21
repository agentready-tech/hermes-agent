"""Turn-scoped secret input for local viewers and messaging adapters.

Main's classic CLI registered ``set_secret_capture_callback`` in process: a skill whose
``required_environment_variables`` are missing opened the masked "Skill Setup Required" panel and the
value was saved to the profile ``.env`` (``save_env_value_secure``) without ever reaching the model.
On the session authority the skill runs in the gateway, where a gateway surface only got the
"secret entry is not available" hint. For a local viewer's turn the owner now sends main's ``secret``
server→client request (``tui_gateway/contracts/server_requests.py``: ``SecretRequestParams`` →
``ValueResult``) to the attached viewers, saves a non-empty answer in this profile's ``.env`` here,
and returns main's callback result. OAuth can consume the same protected reply without saving it.
"""
from __future__ import annotations

import contextlib
import asyncio
import logging
import time
from concurrent.futures import TimeoutError
from contextvars import ContextVar

logger = logging.getLogger(__name__)

SECRET_TIMEOUT_S = 120  # main's CLI secret panel deadline (hermes_cli/callbacks.py)
_turn_secret_prompt = ContextVar("turn_secret_prompt", default=None)


def current_secret_prompt():
    """Return the current turn's protected input, if its channel supports it."""
    return _turn_secret_prompt.get()


def _skipped(var_name, reason, message):
    return {"success": True, "reason": reason, "stored_as": var_name, "validated": False,
            "skipped": True, "message": message}


@contextlib.contextmanager
def _bind_prompt(request_value, *, report_storage=False):
    from tools.skills_tool import bind_turn_secret_capture

    def capture(var_name, prompt, metadata=None):
        value = request_value(var_name, prompt, metadata)
        if not value:
            return _skipped(var_name, "cancelled", "Secret setup was skipped.")
        from hermes_cli.config import save_env_value_secure

        result = {**save_env_value_secure(var_name, value), "skipped": False}
        if report_storage:
            result["message"] = "Secret stored securely. The secret value was not exposed to the model."
        return result

    token = _turn_secret_prompt.set(request_value)
    try:
        with bind_turn_secret_capture(capture):
            yield
    finally:
        _turn_secret_prompt.reset(token)


@contextlib.contextmanager
def secret_prompt_scope(turn, agent):
    """Bind this turn's secret-capture callback (a ContextVar, so parallel tool workers inherit it)."""
    from gateway.run_turn_progress import local_viewer_turn
    if not local_viewer_turn(turn):
        with _adapter_secret_scope(turn, agent):
            yield
        return
    authority, session_id, _generation = turn._approval_owner

    def request_value(var_name, prompt, metadata=None, *, timeout=SECRET_TIMEOUT_S, cancelled=lambda: False):
        from gateway.session_sudo_prompt import ask_viewer
        live = authority.sessions.get(session_id)
        if live is None or cancelled():
            return ""
        params = {"env_var": var_name, "prompt": prompt, **({"metadata": metadata} if metadata else {})}
        return ask_viewer(live.event_stream, session_id, "secret", params, timeout=timeout,
                          cancelled=lambda: cancelled() or bool(getattr(agent, "_interrupt_requested", False)))

    with _bind_prompt(request_value, report_storage=True):
        yield


@contextlib.contextmanager
def _adapter_secret_scope(turn, agent):
    """Use an adapter's existing out-of-band secret transport, never ordinary input."""
    source = turn._ctx.source
    runner = getattr(turn, "_runner", None)
    delivery = getattr(runner, "_delivery_adapter_for", None)
    adapter = delivery(source) if delivery else None
    request = getattr(type(adapter), "request_secret", None)
    loop = getattr(turn._ctx, "_loop_for_step", None)
    if request is None or loop is None:
        yield
        return

    def request_value(var_name, prompt, metadata=None, *, timeout=SECRET_TIMEOUT_S, cancelled=lambda: False):
        current = getattr(turn._ctx, "_run_still_current", lambda: True)

        def available():
            return current() and not getattr(agent, "_interrupt_requested", False) and not cancelled()

        if not available():
            return ""
        future = asyncio.run_coroutine_threadsafe(
            request(adapter, source, label=prompt, site=(metadata or {}).get("help") or var_name), loop)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and available():
                try:
                    value = future.result(timeout=0.25)
                    return value if available() else ""
                except TimeoutError:
                    continue
        except Exception:  # health: allow BLE001 -- adapter SDK errors may contain secret response bodies and must not be logged
            # Adapter exceptions may contain SDK response bodies; never expose them.
            return ""
        finally:
            future.cancel()
        return ""

    with _bind_prompt(request_value):
        yield
