"""Shared-browser tabs must be owned before automatic cleanup can close them."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tools import browser_tab_runtime as runtime
from tools.bot_desktop import lease
from tools.browser_tab_harness import managed_tabs, wrap_code
from tools.browser_tab_resources import TabResources, close_finished_tabs


class Browser:
    def __init__(self):
        self.tabs = {"human": {"targetId": "human", "type": "page"}}
        self.current = "human"
        self.closed = []
        self.safe = {}

    def cdp(self, method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": list(self.tabs.values())}
        if method == "Target.createTarget":
            target = "owned-" + str(len(self.tabs))
            self.tabs[target] = {"targetId": target, "type": "page"}
            return {"targetId": target}
        if method == "Target.attachToTarget":
            return {"sessionId": kw["targetId"]}
        if method == "Runtime.evaluate":
            return {"result": {"value": self.safe.get(kw["session_id"], True)}}
        if method == "Target.closeTarget":
            self.closed.append(kw["targetId"])
            self.tabs.pop(kw["targetId"])
            return {"success": True}
        return {}


@pytest.fixture
def browser(monkeypatch):
    monkeypatch.setattr("tools.browser_tab_harness._driver_created_after", lambda *args: True)
    b = Browser()
    helpers = ModuleType("browser_harness.helpers")
    helpers.cdp = b.cdp
    helpers._send = lambda request: {"ok": True}
    helpers.current_tab = lambda: {"targetId": b.current}
    helpers.switch_tab = lambda target: setattr(b, "current", target)
    package = ModuleType("browser_harness")
    package.helpers = helpers
    monkeypatch.setitem(sys.modules, "browser_harness", package)
    monkeypatch.setitem(sys.modules, "browser_harness.helpers", helpers)
    return b, helpers


def test_harness_tracks_explicit_creation_and_owned_popups_only(tmp_path, browser):
    b, helpers = browser
    ns = {"cdp": b.cdp}
    with managed_tabs(ns, str(tmp_path), "browser", "task"):
        owned = b.current
        b.tabs["popup"] = {"targetId": "popup", "type": "page", "openerId": owned}
        b.tabs["human-new"] = {"targetId": "human-new", "type": "page"}
        assert owned != "human"
    assert helpers.cdp == b.cdp
    r = TabResources(str(tmp_path), "browser", "task")
    with r.locked():
        assert set(r.tabs) == {owned, "popup"}
        assert close_finished_tabs(r, b.cdp) == 2
    assert set(b.tabs) == {"human", "human-new"}


def test_keep_drafts_and_other_tasks_survive(tmp_path, browser):
    b, _ = browser
    ns = {"cdp": b.cdp}
    with managed_tabs(ns, str(tmp_path), "browser", "task"):
        kept = b.current
        ns["keep_tab"]()
        draft = ns["cdp"]("Target.createTarget")["targetId"]
        b.safe[draft] = False
    with managed_tabs(ns, str(tmp_path), "browser", "sibling"):
        sibling = b.current
    r = TabResources(str(tmp_path), "browser", "task")
    with r.locked():
        assert close_finished_tabs(r, b.cdp) == 0
    assert {kept, draft, sibling, "human"} == set(b.tabs)


def test_cleanup_is_idempotent_and_browser_identity_isolated(tmp_path, browser):
    b, _ = browser
    with managed_tabs({}, str(tmp_path), "browser", "task"):
        pass
    r = TabResources(str(tmp_path), "other-browser", "task")
    with r.locked():
        assert close_finished_tabs(r, b.cdp) == 0
    r = TabResources(str(tmp_path), "browser", "task")
    with r.locked():
        assert close_finished_tabs(r, b.cdp) == 1
        assert close_finished_tabs(r, b.cdp) == 0


def test_human_takeover_even_if_handed_back_blocks_old_tab_cleanup(tmp_path, browser):
    b, _ = browser
    with managed_tabs({}, str(tmp_path), "browser", "task"):
        pass
    path = tmp_path / "bot-desktop" / "lease.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for holder, epoch in [("human", 1), ("agent", 2)]:
        path.write_text(json.dumps({"holder": holder, "epoch": epoch}))
        r = TabResources(str(tmp_path), "browser", "task")
        with r.locked():
            assert close_finished_tabs(r, b.cdp) == 0
    assert not b.closed


def test_human_lease_refuses_harness_work(tmp_path, browser):
    path = tmp_path / "bot-desktop" / "lease.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"holder": "human"}))
    with pytest.raises(lease.HumanHasControl), managed_tabs({}, str(tmp_path), "browser", "task"):
        pytest.fail("must not execute")


def test_wrapper_handles_multiline_and_exception_keeps_owned_tab(tmp_path, browser):
    ns = {}
    code = wrap_code("raise ValueError('test')", home=str(tmp_path), endpoint="browser", task="task")
    with pytest.raises(ValueError):
        exec(code, ns)
    r = TabResources(str(tmp_path), "browser", "task")
    with r.locked():
        assert len(r.tabs) == 1
    assert not browser[0].closed


@pytest.mark.parametrize("completed,interrupted,failed", [(False, False, False), (True, True, False), (True, False, True)])
def test_unfinished_turn_never_closes_tabs(monkeypatch, completed, interrupted, failed):
    monkeypatch.setattr(runtime, "get_hermes_home", lambda: pytest.fail("must not reach a browser"))
    runtime.finish_native_tabs(object(), "task", completed=completed, interrupted=interrupted, failed=failed)


def test_completed_turn_uses_exact_recorded_route_and_preserves_live_work(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "get_hermes_home", lambda: tmp_path)
    import agent.tool_operation_lifecycle as lifecycle
    import tools.browser_tool_session as session
    import tools.browser_use_cli as cli
    live = [True]
    monkeypatch.setattr(lifecycle, "preserve_resources", lambda *args: live[0])
    monkeypatch.setattr(session, "run_fenced", lambda _info, fn: fn())
    calls = []
    monkeypatch.setattr(cli, "_run_cli_killing_process_group", lambda *args: calls.append(args) or SimpleNamespace(returncode=0))
    runtime.prepare_native_tabs("pass", "task", ["harness"], {"BU_CDP_URL": "ws://browser", "BU_NAME": "mine"})
    runtime.finish_native_tabs(object(), "task", completed=True, interrupted=False, failed=False)
    assert not calls
    live[0] = False
    runtime.finish_native_tabs(object(), "task", completed=True, interrupted=False, failed=False)
    assert calls[0][0] == ["harness"] and calls[0][2]["BU_NAME"].startswith("hermes-")
    runtime.finish_native_tabs(object(), "task", completed=True, interrupted=False, failed=False)
    assert len(calls) == 1


def test_native_exec_creates_owned_tab_and_success_cleanup_reaches_harness(monkeypatch, tmp_path, browser):
    """The real browser_exec entry point must install the lifecycle, not just helpers."""
    import subprocess
    import tools.browser_use_cli as cli
    import tools.browser_tool_session as session
    import agent.tool_operation_lifecycle as lifecycle
    b, helpers = browser
    monkeypatch.setattr(cli, "_find_cli", lambda: ["harness"])
    monkeypatch.setattr(cli, "_base_subprocess_env", dict)
    monkeypatch.setattr(cli, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(cli, "_workspace_dir", lambda *args: None)
    monkeypatch.setattr(cli, "_find_screenshot", lambda *args: None)
    monkeypatch.setattr(runtime, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(session, "run_fenced", lambda _info, fn: fn())
    monkeypatch.setattr(lifecycle, "preserve_resources", lambda *args: False)
    def route(env, *args):
        env.update(BU_CDP_URL="ws://isolated-fixture", _HERMES_BU_BOT_DESKTOP_BROWSER="1")
        return None, False
    monkeypatch.setattr(cli, "_route_exec", route)
    def execute(cmd, code, env, timeout):
        # The actual harness reuses an already blank attached tab.
        exec(code, {"cdp": helpers.cdp, "new_tab": lambda url: b.current})
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(cli, "_run_cli_killing_process_group", execute)
    cli.browser_exec('new_tab("about:blank")', task_id="boundary")
    assert b.current != "human", "must not adopt the existing user tab"
    runtime.finish_native_tabs(object(), "boundary", completed=True, interrupted=False, failed=False)
    assert set(b.tabs) == {"human"}


@pytest.mark.parametrize("protection", [None, "draft", "human-takeover", "human-during-cleanup"])
def test_finished_native_task_retires_its_driver_tab_without_closing_protected_work(
    monkeypatch, tmp_path, browser, protection,
):
    """The named harness creates its own tab before managed_tabs sees any CDP calls."""
    import subprocess
    import tools.browser_use_cli as cli
    import tools.browser_tool_session as session
    import agent.tool_operation_lifecycle as lifecycle
    b, helpers = browser
    b.tabs["driver-startup"] = {"targetId": "driver-startup", "type": "page"}
    b.tabs["sibling"] = {"targetId": "sibling", "type": "page"}
    b.current = "driver-startup"
    shutdowns = []

    def driver_request(request):
        assert request == {"meta": "shutdown"}
        shutdowns.append(request)
        b.tabs.pop("driver-startup")
        return {"ok": True}

    helpers._send = driver_request
    monkeypatch.setattr(cli, "_find_cli", lambda: ["harness"])
    monkeypatch.setattr(cli, "_base_subprocess_env", dict)
    monkeypatch.setattr(cli, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(cli, "_workspace_dir", lambda *args: None)
    monkeypatch.setattr(cli, "_find_screenshot", lambda *args: None)
    monkeypatch.setattr(runtime, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(session, "run_fenced", lambda _info, fn: fn())
    monkeypatch.setattr(lifecycle, "preserve_resources", lambda *args: False)

    def route(env, *args):
        env.update(BU_CDP_URL="ws://driver-fixture", _HERMES_BU_BOT_DESKTOP_BROWSER="1")
        return None, False

    def execute(cmd, code, env, timeout):
        exec(code, {"cdp": helpers.cdp})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "_route_exec", route)
    monkeypatch.setattr(cli, "_run_cli_killing_process_group", execute)
    cli.browser_exec("pass", task_id="driver-retirement")
    owned = b.current
    if protection == "draft":
        b.safe[owned] = False
    elif protection == "human-takeover":
        path = tmp_path / "bot-desktop" / "lease.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"holder": "agent", "epoch": 2}))
        # A later call must not erase the driver's original human-control epoch.
        cli.browser_exec("pass", task_id="driver-retirement")
    elif protection == "human-during-cleanup":
        def takeover_after_close(method, **params):
            result = b.cdp(method, **params)
            if method == "Target.closeTarget":
                path = tmp_path / "bot-desktop" / "lease.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"holder": "agent", "epoch": 2}))
            return result
        helpers.cdp = takeover_after_close
    runtime.finish_native_tabs(object(), "driver-retirement", completed=True, interrupted=False, failed=False)
    if protection:
        expected = {"human", "sibling", "driver-startup"}
        if protection != "human-during-cleanup":
            expected.add(owned)
        assert expected <= set(b.tabs)
        assert not shutdowns
    else:
        assert set(b.tabs) == {"human", "sibling"}
        assert len(shutdowns) == 1
