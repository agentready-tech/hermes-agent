"""Bind the existing vault prompts to an adapter's out-of-band secret input."""

import asyncio
import contextlib
from concurrent.futures import TimeoutError

from agent.vault_backends import unlock


@contextlib.contextmanager
def vault_prompt_scope(turn, agent):
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
