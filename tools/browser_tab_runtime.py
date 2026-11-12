"""Connect successful task finalization to the exact native harness it drove."""
from __future__ import annotations

import hashlib
import threading

from hermes_constants import get_hermes_home
from tools.browser_tab_harness import wrap_code

_contexts: dict[tuple[str, str], dict[str, tuple]] = {}
_completed_tasks: set[tuple[str, str]] = set()
_lock = threading.RLock()


def _driver_process_snapshot():
    import psutil
    return {process.pid: process.info['create_time']
            for process in psutil.process_iter(['create_time'], ad_value=None)}


def _close_idle_user_tabs(home, task):
    from hermes_cli.config import read_raw_config
    browser = read_raw_config().get('browser') or {}
    if not isinstance(browser, dict) or browser.get('close_idle_user_tabs') is not True:
        return False
    with _lock:
        if any(other_home == home and other_task != task and contexts
               and (other_home, other_task) not in _completed_tasks
               for (other_home, other_task), contexts in _contexts.items()):
            return False
    from hermes_runtime.adapters.hermes import enabled, managed_home
    if enabled():
        from hermes_runtime.adapters.tool_operations import ToolOperationStore
        if ToolOperationStore(managed_home()).has_running_resource('desktop'):
            return False
    return True


def prepare_native_tabs(code: str, task: str, cmd: list, env: dict, *, generation: str = '') -> str:
    home = str(get_hermes_home())
    endpoint = env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or ""
    if not endpoint or (not task and not generation):
        return code
    # Keep credentials in this process only; the durable ledger receives a hash.
    identity = endpoint + '\0' + generation if generation else endpoint
    browser = hashlib.sha256(identity.encode()).hexdigest()
    # The harness daemon name is global to the unix user. Scope it to both
    # profile and task so a sibling cannot switch its attachment mid-operation.
    name = "hermes-" + hashlib.sha256((home + "\0" + task + "\0" + env.get("BU_NAME", "default") + "\0" + browser).encode()).hexdigest()[:32]
    env["BU_NAME"] = name
    if not task:
        return code
    from tools.bot_desktop import lease
    with _lock:
        _completed_tasks.discard((home, task))
        contexts = _contexts.setdefault((home, task), {})
        previous = contexts.get(name)
        # A later call after human takeover must not re-own the driver's startup tab.
        epoch, driver_before = previous[3:] if previous else (lease.get(home).epoch, _driver_process_snapshot())
        contexts[name] = (list(cmd), dict(env), browser, epoch, driver_before)
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
        if contexts:
            # Keep failed old-endpoint cleanup retryable without treating its
            # already completed owner as a live child forever.
            _completed_tasks.add(key)
    if not contexts:
        return
    close_user_tabs = _close_idle_user_tabs(home, task)
    for name, (cmd, env, browser, epoch, driver_before) in contexts.items():
        code = wrap_code("pass", home=home, endpoint=browser, task=task, cleanup=True,
                         driver_epoch=epoch, driver_before=driver_before, close_user_tabs=close_user_tabs)
        result = run_fenced({"features": {"local": True}},
                            lambda: _run_cli_killing_process_group(cmd, code, env, 15))
        if getattr(result, "returncode", None) == 0:
            with _lock:
                _contexts.get(key, {}).pop(name, None)
    with _lock:
        if not _contexts.get(key):
            _contexts.pop(key, None)
            _completed_tasks.discard(key)
