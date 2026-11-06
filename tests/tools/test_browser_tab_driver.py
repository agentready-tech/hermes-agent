"""Use the installed harness processes; simulate only Chrome's CDP transport."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from tests.tools.test_browser_cdp_tool import _CDPServer
from tests.waiting import wait_until
from tools import browser_tab_runtime as runtime
from tools import browser_use_cli as cli


@pytest.fixture
def native_drivers(monkeypatch, tmp_path):
    import agent.tool_operation_lifecycle as lifecycle
    import tools.browser_tool_session as session
    tabs = {"human": {"targetId": "human", "type": "page", "url": "about:blank", "title": ""}}
    server = _CDPServer()
    created = []

    def create(params, sid):
        target = "created-" + str(len(created))
        created.append(target)
        tabs[target] = {"targetId": target, "type": "page", "url": "about:blank", "title": ""}
        return {"targetId": target}

    def close(params, sid):
        tabs.pop(params["targetId"])
        return {"success": True}

    server.on("Target.getTargets", lambda p, s: {"targetInfos": list(tabs.values())})
    server.on("Target.createTarget", create)
    server.on("Target.closeTarget", close)
    server.on("Target.attachToTarget", lambda p, s: {"sessionId": "session-" + p["targetId"]})
    server.on("Target.getTargetInfo", lambda p, s: {"targetInfo": tabs[p["targetId"]]})
    server.on("Runtime.evaluate", lambda p, s: {"result": {"type": "boolean", "value": True}})
    for method in ("Page.enable", "DOM.enable", "Runtime.enable", "Network.enable", "Target.detachFromTarget"):
        server.on(method, lambda p, s: {})
    endpoint = server.start()
    command = [sys.executable, "-c", "from browser_harness import run; run.print_update_banner = lambda: None; run.main()"]
    envs = []
    with tempfile.TemporaryDirectory(prefix="hermes-tabs-", dir="/tmp") as ipc:
        def base_env():
            env = {**os.environ, "BH_HOME": str(tmp_path / "harness"), "BH_RUNTIME_DIR": ipc,
                   "BH_RUNTIME_DIR_SHARED": "1", "BU_CDP_WS": endpoint,
                   "_HERMES_BU_BOT_DESKTOP_BROWSER": "1"}
            envs.append(env)
            return env

        monkeypatch.setattr(cli, "_base_subprocess_env", base_env)
        monkeypatch.setattr(cli, "_find_cli", lambda: command)
        monkeypatch.setattr(cli, "_route_exec", lambda *args: (None, False))
        monkeypatch.setattr(cli, "_attach_vault_supervisor", lambda *args: None)
        monkeypatch.setattr(cli, "_workspace_dir", lambda *args: None)
        monkeypatch.setattr(runtime, "get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv('HERMES_HOME', str(tmp_path))
        monkeypatch.setattr(session, "run_fenced", lambda info, fn: fn())
        monkeypatch.setattr(lifecycle, "preserve_resources", lambda *args: False)
        try:
            yield tabs
        finally:
            for env in envs:
                subprocess.run(command + ["--reload"], env=env, capture_output=True, timeout=15, check=False)
            server.stop()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("runtime_restarted", [False, True])
@pytest.mark.parametrize('close_user_tabs', [False, True])
def test_actual_named_driver_shutdown_removes_its_startup_tab_and_preserves_sibling(
    native_drivers, runtime_restarted, close_user_tabs, monkeypatch,
):
    from hermes_cli import config
    monkeypatch.setattr(config, 'read_raw_config', lambda: {'browser': {'close_idle_user_tabs': close_user_tabs}})
    tabs = native_drivers
    for task in ("parent", "child"):
        result = json.loads(cli.browser_exec("pass", task_id=task))
        assert result["success"], result
    assert len(tabs) == 5, "Each real driver has a startup tab plus its tracked task tab"
    if runtime_restarted:
        # The gateway lost its in-memory admission; the existing driver may have
        # been handed to a human before this new runtime started.
        runtime._contexts.pop((str(runtime.get_hermes_home()), "parent"))
        assert json.loads(cli.browser_exec("pass", task_id="parent"))["success"]
    before = set(tabs)
    runtime.finish_native_tabs(object(), "parent", completed=True, interrupted=False, failed=False)
    wait_until(lambda: len(tabs) == (4 if runtime_restarted else 3), what="parent driver's native shutdown")
    child_tabs = set(tabs)
    assert "human" in child_tabs and child_tabs < before
    result = json.loads(cli.browser_exec("pass", task_id="child"))
    assert result["success"], result
    assert set(tabs) == child_tabs, "The child's live driver and tab remain usable"
    runtime.finish_native_tabs(object(), "child", completed=True, interrupted=False, failed=False)
    remaining = 0 if close_user_tabs else (2 if runtime_restarted else 1)
    wait_until(lambda: len(tabs) == remaining, what="child driver's native shutdown")
    assert ('human' in tabs) is not close_user_tabs
