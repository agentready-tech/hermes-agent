"""Bind the existing vault prompts to an adapter's out-of-band secret input."""

import asyncio
import contextlib
import json
from concurrent.futures import TimeoutError

from agent.vault_backends import unlock


@contextlib.contextmanager
def vault_prompt_scope(turn, agent):
    from gateway.run_turn_progress import local_viewer_turn
    if local_viewer_turn(turn):
        with _viewer_vault_scope(turn, agent):
            yield
        return
    source = turn._ctx.source
    adapter = turn._runner._delivery_adapter_for(source)
    # An optional transport capability; no Telegram types or routing policy in the core.
    request = getattr(type(adapter), "request_secret", None)
    loop = turn._ctx._loop_for_step
    if request is None or loop is None:
        yield
        return

    def cancelled():
        return bool(getattr(agent, "_interrupt_requested", False)) or not turn._ctx._run_still_current()

    def ask(label, site):
        if cancelled():
            return ""
        future = asyncio.run_coroutine_threadsafe(request(adapter, source, label=label, site=site), loop)
        try:
            while not cancelled():
                try:
                    return future.result(timeout=0.25) or ""
                except TimeoutError:
                    continue
            return ""
        except Exception:  # health: allow BLE001 -- adapter errors may contain the secret; never expose them to the model
            # Transport errors must never echo a request/response body into a tool result.
            return ""
        finally:
            future.cancel()

    def save_login(origin, site):
        identifier = ask("Логин или email (будет виден агенту)", origin)
        if not identifier:
            return None
        password = ask("Пароль", origin)
        return {"identifier": identifier, "password": password} if password else None

    previous = (unlock.get_save_login_prompt_callback(), unlock.get_code_prompt_callback())
    unlock.set_save_login_prompt_callback(save_login)
    unlock.set_code_prompt_callback(lambda site, hint: ask("Код подтверждения", site))
    try:
        yield
    finally:
        unlock.set_save_login_prompt_callback(previous[0])
        unlock.set_code_prompt_callback(previous[1])


@contextlib.contextmanager
def _viewer_vault_scope(turn, agent):
    """The same masked native forms as the TUI, answered through the existing owner socket."""
    from gateway.session_sudo_prompt import ask_viewer
    authority, session_id, _generation = turn._approval_owner
    events = authority.sessions[session_id].event_stream

    def ask(method, params, timeout=180):
        return ask_viewer(events, session_id, method, params, timeout=timeout,
                          cancelled=lambda: bool(getattr(agent, '_interrupt_requested', False)))

    def save_login(origin, site):
        raw = ask('vault.save_login', {'origin': origin, 'site': site})
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            return None
        return value if isinstance(value, dict) and value.get('password') else None

    previous = (unlock.get_unlock_prompt_callback(), unlock.get_save_login_prompt_callback(),
                unlock.get_code_prompt_callback())
    unlock.set_unlock_prompt_callback(lambda backend, display_name: ask('vault.unlock_prompt',
        {'backend': backend, 'display_name': display_name}, timeout=120))
    unlock.set_save_login_prompt_callback(save_login)
    unlock.set_code_prompt_callback(lambda site, hint: ask('vault.code', {'site': site, 'hint': hint}))
    try:
        yield
    finally:
        unlock.set_unlock_prompt_callback(previous[0])
        unlock.set_save_login_prompt_callback(previous[1])
        unlock.set_code_prompt_callback(previous[2])
