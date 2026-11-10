"""Harness-side resource hooks, installed only on the native shared desktop."""
from __future__ import annotations

import contextlib
import logging

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


def _driver_started_since(helpers, before):
    import psutil
    if before is None:
        return False
    process = psutil.Process(helpers._send({"meta": "ping"})["pid"])
    # Linux process birth uses kernel boot time, which can lag time.time().
    # Compare process generations, never a wall-clock admission timestamp.
    return process.pid not in before or (before[process.pid] is not None
                                         and before[process.pid] != process.create_time())


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


@contextlib.contextmanager
def managed_tabs(namespace: dict, home: str, endpoint: str, task: str, *, cleanup=False,
                 driver_epoch=None, driver_before=None, close_user_tabs=False):
    from browser_harness import helpers
    resources = TabResources(home, endpoint, task)
    original = helpers.cdp
    old_globals = {key: namespace.get(key) for key in ("cdp", "keep_tab", "new_tab")}
    admitted = None
    retired = False

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

    with resources.locked():
        try:
            from tools.bot_desktop.lease import assert_agent_may_act
            admitted = assert_agent_may_act(home)
            resources.admission_epoch = admitted.epoch
            helpers.cdp = namespace["cdp"] = tracked
            namespace["keep_tab"] = keep_tab
            if cleanup:
                close_finished_tabs(resources, original, close_unowned=close_user_tabs)
            else:
                # Never steer the first call onto an existing human/sibling tab.
                try:
                    current = helpers.current_tab()["targetId"]
                except (RuntimeError, OSError, KeyError, ValueError):
                    current = None
                row = resources.tabs.get(current, {})
                if row.get("task") != task or row.get("keep"):
                    target = tracked("Target.createTarget", url="about:blank", background=True)["targetId"]
                    helpers.switch_tab(target)
            yield
            if cleanup:
                retired = _retire_driver(resources, helpers, driver_epoch, driver_before, close_user_tabs)
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
