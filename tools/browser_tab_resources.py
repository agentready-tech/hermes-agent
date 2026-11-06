"""Task ownership and configured idle-tab cleanup for the shared Bot Desktop.

The ledger contains target IDs, not URLs or page contents. Existing tabs are never
adopted; their optional cleanup follows profile policy. One browser lock serializes harness operations and turn-end cleanup;
human lease changes invalidate admission, including an intervening takeover.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path

from tools.bot_desktop import lease


class TabResources:
    def __init__(self, home: str, endpoint: str, task: str):
        self.home, self.task = home, task
        self.admission_epoch = lease.get(home).epoch
        self.retained_unowned = False
        key = hashlib.sha256(endpoint.encode()).hexdigest()
        self.path = Path(home) / "cache" / "browser-tabs" / (key + ".json")
        self.tabs: dict[str, dict] = {}

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.tabs = json.loads(self.path.read_text(encoding="utf-8-sig")) if self.path.exists() else {}
                if not isinstance(self.tabs, dict):
                    raise ValueError("Invalid browser ownership ledger")
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as out:
            os.chmod(tmp, 0o600)
            json.dump(self.tabs, out)
        tmp.replace(self.path)

    def created(self, target: str, *, epoch=None, keep=False):
        self.tabs[target] = {"task": self.task, "keep": keep,
                             "epoch": lease.get(self.home).epoch if epoch is None else epoch}
        self.save()  # durable even if the exec later times out

    def keep(self, target: str):
        row = self.tabs.get(target)
        if row and row["task"] == self.task:
            row["keep"] = True
            self.save()

    def may_close(self, target: str, *, close_unowned=False) -> bool:
        row = self.tabs.get(target)
        current = lease.get(self.home)
        if current.holder != lease.AGENT or current.epoch != self.admission_epoch:
            return False
        if row is None:
            return close_unowned
        return bool(row["task"] == self.task and not row["keep"]
                    and (row["epoch"] == current.epoch or close_unowned))


# Read only a safety verdict, never return field contents or URLs to the ledger.
# GET/search forms are repeatable; POST forms, passwords, editable drafts and
# media/capture must survive a completed chat turn until explicitly handled.
SAFE_TO_CLOSE = """(() => {
  const fields = [...document.querySelectorAll('input,textarea,[contenteditable="true"]')];
  const draft = fields.some(e => e.type === 'password' || e.autocomplete === 'one-time-code' ||
    (e.isContentEditable && e.textContent.trim()) ||
    (e.tagName === 'TEXTAREA' && e.value.trim()) ||
    (e.form?.method.toLowerCase() === 'post' && !['hidden','submit','button','search'].includes(e.type)
      && (e.type === 'checkbox' || e.type === 'radio' ? e.checked : e.value.trim())));
  const media = [...document.querySelectorAll('audio,video')].some(e => !e.paused);
  return !draft && !media && !document.fullscreenElement;
})()"""


def close_finished_tabs(resources: TabResources, cdp, *, close_unowned=False) -> int:
    """Close safe task tabs and optionally idle user tabs; never a sibling's."""
    infos = cdp("Target.getTargets").get("targetInfos", [])
    live = {t["targetId"] for t in infos if t.get("type") == "page"}
    for target in list(resources.tabs):
        if target not in live:
            resources.tabs.pop(target)
    closed = 0
    resources.retained_unowned = False
    for target in live:
        if not resources.may_close(target, close_unowned=close_unowned):
            continue
        sid = None
        removed = False
        try:
            sid = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
            result = cdp("Runtime.evaluate", session_id=sid, expression=SAFE_TO_CLOSE,
                         returnByValue=True).get("result", {})
            if result.get("value") is True and resources.may_close(target, close_unowned=close_unowned):
                if cdp("Target.closeTarget", targetId=target).get("success"):
                    resources.tabs.pop(target, None)
                    closed += 1
                    removed = True
        except (RuntimeError, OSError, KeyError, ValueError) as error:
            logging.getLogger(__name__).debug("Keeping an unavailable browser tab (%s)", type(error).__name__)
        finally:
            if not removed and target not in resources.tabs:
                resources.retained_unowned = True
            if sid:
                with contextlib.suppress(RuntimeError, OSError, KeyError, ValueError):
                    cdp("Target.detachFromTarget", sessionId=sid)
    resources.save()
    return closed
