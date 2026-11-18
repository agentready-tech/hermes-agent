"""Harness-side resource hooks, installed only on the native shared desktop."""
from __future__ import annotations

import contextlib
import logging
import os

from tools.browser_tab_resources import TabResources, close_finished_tabs


def _record_popups(resources, cdp):
    infos = cdp("Target.getTargets").get("targetInfos", [])
    for _ in infos:
        added = False
        for item in infos:
            target = item["targetId"]
            parent = resources.tabs.get(item.get("openerId"), {})
            if item.get("type") == "page" and target not in resources.tabs and parent.get("task") == resources.task:
                resources.created(target, epoch=parent["epoch"], keep=parent["keep"])
                added = True
        if not added:
            break


def _driver_identity(helpers):
    import psutil
    process = psutil.Process(helpers._send({"meta": "ping"})["pid"])
    return process.pid, process.create_time()


def _driver_started_since(helpers, before):
    if before is None:
        return False
    pid, started = _driver_identity(helpers)
    # Linux process birth uses kernel boot time, which can lag time.time().
    # Compare process generations, never a wall-clock admission timestamp.
    return pid not in before or (before[pid] is not None and before[pid] != started)


def _retire_driver(resources, helpers, epoch, driver_before, close_user_tabs=False):
    from tools.bot_desktop import lease
    if any(row.get("task") == resources.task for row in resources.tabs.values()):
        return False
    # A daemon left by an earlier gateway has no current admission proof.
    if close_user_tabs and resources.retained_unowned:
        return False
    if not close_user_tabs and not _driver_started_since(helpers, driver_before):
        return False
    current = lease.get(resources.home)
    expected_epoch = resources.admission_epoch if close_user_tabs else epoch
    if current.holder != lease.AGENT or expected_epoch != current.epoch:
        return False
    # Native shutdown closes the daemon's exact startup tab and stops recovery.
    if helpers._send({"meta": "shutdown"}).get("ok") is not True:
        raise RuntimeError("Browser task driver did not acknowledge shutdown")
    return True


def _unused_bootstrap(resources, helpers, cdp, target):
    if not target or not resources.may_close(target):
        return False
    with contextlib.suppress(RuntimeError, OSError, KeyError, ValueError):
        current = helpers.current_tab()
        if current.get("targetId") != target or current.get("url") != "about:blank":
            return False
        infos = cdp("Target.getTargets").get("targetInfos", [])
        if any(info.get("openerId") == target for info in infos):
            return False
        result = cdp("Runtime.evaluate", returnByValue=True, expression=(
            "document.URL === 'about:blank' && history.length === 1 && document.body?.innerHTML === ''"
        ))
        return result.get("result", {}).get("value") is True
    return False


@contextlib.contextmanager
def managed_tabs(namespace: dict, home: str, endpoint: str, task: str, *, cleanup=False,
                 driver_epoch=None, driver_before=None, close_user_tabs=False):
    from browser_harness import helpers
    resources = TabResources(home, endpoint, task, os.environ.get("BU_NAME", ""))
    original = helpers.cdp
    original_new_tab = helpers.new_tab
    original_switch_tab = helpers.switch_tab
    old_globals = {key: namespace.get(key) for key in ("cdp", "keep_tab", "new_tab", "switch_tab")}
    admitted = None
    retired = False
    bootstrap = None

    def tracked(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        if method == "Target.createTarget" and result.get("targetId"):
            resources.created(result["targetId"], epoch=admitted.epoch)
        elif method == "Target.closeTarget" and result.get("success"):
            resources.tabs.pop(kwargs.get("targetId"), None)
            resources.save()
        return result

    def keep_tab(target=None):
        """Retain a result or a page waiting for human input across completed turns."""
        if isinstance(target, dict):
            target = target.get("targetId") or target.get("target_id")
        resources.keep(target or helpers.current_tab()["targetId"])

    def switch_tab(target, activate=None):
        target_id = (target.get("targetId") or target.get("target_id")) if isinstance(target, dict) else target
        row = resources.tabs.get(target_id)
        if activate is None:
            activate = row is None or row.get("task") == task
        close_bootstrap = row is None and activate and _unused_bootstrap(resources, helpers, original, bootstrap)
        result = original_switch_tab(target, activate=activate)
        if row is None:
            resources.borrow(target_id, _driver_identity(helpers), activate)
        if close_bootstrap and resources.may_close(bootstrap):
            with contextlib.suppress(RuntimeError, OSError, KeyError, ValueError):
                tracked("Target.closeTarget", targetId=bootstrap)
        return result

    def new_tab(url="about:blank"):
        if url != "about:blank" and not resources.borrowed_tabs:
            return original_new_tab(url)
        current = {}
        with contextlib.suppress(RuntimeError, OSError, KeyError, ValueError):
            current = helpers.current_tab()
        target = current.get("targetId")
        if url != "about:blank" and target in resources.borrowed_tabs:
            # The harness normally reuses any attached blank. An explicit
            # new_tab must leave a borrowed blank in the owner's hands.
            fresh = original_new_tab()
            helpers.goto_url(url)
            return fresh
        if url == "about:blank" and current.get("url") == "about:blank":
            row = resources.tabs.get(target, {})
            if row.get("task") == task and not row.get("keep"):
                return target
        return original_new_tab(url)

    with resources.locked():
        try:
            from tools.bot_desktop.lease import assert_agent_may_act
            admitted = assert_agent_may_act(home)
            resources.admission_epoch = admitted.epoch
            resources.load_borrowed(lambda: _driver_identity(helpers))
            helpers.cdp = namespace["cdp"] = tracked
            namespace["keep_tab"] = keep_tab
            helpers.new_tab = namespace["new_tab"] = new_tab
            helpers.switch_tab = namespace["switch_tab"] = switch_tab
            if cleanup:
                close_finished_tabs(resources, original, close_unowned=close_user_tabs)
            else:
                try:
                    current_tab = helpers.current_tab()
                    current = current_tab["targetId"]
                except (RuntimeError, OSError, KeyError, ValueError):
                    current_tab = {}
                    current = None
                row = resources.tabs.get(current, {})
                # The newly launched named daemon already created its own blank tab.
                # An attached tab from an older daemon could belong to a human or sibling.
                if (current and not row and current_tab.get("url") == "about:blank"
                        and driver_epoch == admitted.epoch and _driver_started_since(helpers, driver_before)):
                    resources.created(current, epoch=admitted.epoch)
                    bootstrap = current
                    original("Target.activateTarget", targetId=current)
                elif row.get("task") == task and not row.get("keep"):
                    original("Target.activateTarget", targetId=current)
                elif not row and current in resources.borrowed_tabs:
                    if resources.borrowed_tabs[current]:
                        original("Target.activateTarget", targetId=current)
                else:
                    target = tracked("Target.createTarget", url="about:blank", background=True)["targetId"]
                    switch_tab(target)
                    bootstrap = target
            yield
            if cleanup:
                retired = _retire_driver(resources, helpers, driver_epoch, driver_before, close_user_tabs)
                resources.borrowed_path.unlink(missing_ok=True)
        finally:
            # Popups created by an owned page share its owner; a concurrently
            # opened human tab has no owned opener and is never adopted.
            try:
                from tools.bot_desktop.lease import get
                if not retired and admitted is not None and get(home).epoch == admitted.epoch:
                    _record_popups(resources, original)
            except (RuntimeError, OSError, KeyError, ValueError) as error:
                logging.getLogger(__name__).debug("Popup ownership reconciliation deferred (%s)", type(error).__name__)
            helpers.cdp = original
            helpers.new_tab = original_new_tab
            helpers.switch_tab = original_switch_tab
            for key, value in old_globals.items():
                if value is None:
                    namespace.pop(key, None)
                else:
                    namespace[key] = value


def wrap_code(code: str, *, home: str, endpoint: str, task: str, cleanup=False,
              driver_epoch=None, driver_before=None, close_user_tabs=False) -> str:
    """Use the installed runtime, not the harness package's unrelated site path."""
    from pathlib import Path
    source = str(Path(__file__).resolve().parent.parent)
    prefix = (f"import sys as _hermes_sys\n_hermes_sys.path.insert(0, {source!r})\n"
              "from tools.browser_tab_harness import managed_tabs as _hermes_managed_tabs\n"
              f"with _hermes_managed_tabs(globals(), {home!r}, {endpoint!r}, {task!r}, "
              f"cleanup={cleanup!r}, driver_epoch={driver_epoch!r}, driver_before={driver_before!r}, "
              f"close_user_tabs={close_user_tabs!r}):\n")
    import textwrap
    return prefix + textwrap.indent(code or "pass", "    ") + "\n"
