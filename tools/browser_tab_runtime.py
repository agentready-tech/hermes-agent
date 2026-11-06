"""Connect successful task finalization to the exact native harness it drove."""
from __future__ import annotations

import hashlib
import threading
import time

from hermes_constants import get_hermes_home
from tools.browser_tab_harness import wrap_code

_contexts: dict[tuple[str, str], dict[str, tuple]] = {}
_lock = threading.RLock()


def prepare_native_tabs(code: str, task: str, cmd: list, env: dict) -> str:
    home = str(get_hermes_home())
    endpoint = env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or ""
    if not task or not endpoint:
        return code
    # Keep credentials in this process only; the durable ledger receives a hash.
    browser = hashlib.sha256(endpoint.encode()).hexdigest()
    # The harness daemon name is global to the unix user. Scope it to both
    # profile and task so a sibling cannot switch its attachment mid-operation.
    name = "hermes-" + hashlib.sha256((home + "\0" + task + "\0" + env.get("BU_NAME", "default") + "\0" + browser).encode()).hexdigest()[:32]
    env["BU_NAME"] = name
    from tools.bot_desktop import lease
    with _lock:
        contexts = _contexts.setdefault((home, task), {})
        previous = contexts.get(name)
        # A later call after human takeover must not re-own the driver's startup tab.
        epoch, started_after = previous[3:] if previous else (lease.get(home).epoch, time.time())
        contexts[name] = (list(cmd), dict(env), browser, epoch, started_after)
    return wrap_code(code, home=home, endpoint=browser, task=task)


def finish_native_tabs(agent, task: str, *, completed: bool, interrupted: bool, failed: bool) -> None:
    if not completed or interrupted or failed:
        return
    from agent.tool_operation_lifecycle import preserve_resources
    if preserve_resources(agent, task):
        return
    from tools.browser_tool_session import run_fenced
    from tools.browser_use_cli import _run_cli_killing_process_group
    home = str(get_hermes_home())
    key = home, task
    with _lock:
        contexts = dict(_contexts.get(key, {}))
    for name, (cmd, env, browser, epoch, started_after) in contexts.items():
        code = wrap_code("pass", home=home, endpoint=browser, task=task, cleanup=True,
                         driver_epoch=epoch, driver_started_after=started_after)
        result = run_fenced({"features": {"local": True}},
                            lambda: _run_cli_killing_process_group(cmd, code, env, 15))
        if getattr(result, "returncode", None) == 0:
            with _lock:
                _contexts.get(key, {}).pop(name, None)
    with _lock:
        if not _contexts.get(key):
            _contexts.pop(key, None)
