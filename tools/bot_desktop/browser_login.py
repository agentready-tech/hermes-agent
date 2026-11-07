"""Explicit settings action on the own human-controlled browser; never an agent/model tool."""

import hashlib
import json
import os
import re
import stat
import subprocess

from tools.bot_desktop import lease, runtime


class BrowserLoginError(RuntimeError):
    pass


def _receipt(path):
    owner_uid = getattr(os, "getuid", None)
    if owner_uid is None:
        raise BrowserLoginError("browser_login_unsupported_platform")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner_uid() or info.st_mode & 0o077:
                raise ValueError
            raw = stream.read(2049)
        value = json.loads(raw)
        if (len(raw) > 2048 or not isinstance(value, dict)
                or set(value) != {"viewer_hash", "requested_epoch", "lease_epoch", "phase"}
                or not isinstance(value["viewer_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["viewer_hash"])
                or type(value["requested_epoch"]) is not int or type(value["lease_epoch"]) is not int
                or not isinstance(value["phase"], str) or value["phase"] not in {"attempted", "opened"}):
            raise ValueError
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        raise BrowserLoginError("browser_login_receipt_unavailable") from None


def open_google(viewer_id, expected_epoch):
    from hermes_cli.web_routers.display import connected_viewer_generation
    from tools.bot_desktop import browser
    from utils import atomic_write_text

    if not isinstance(viewer_id, str) or not 1 <= len(viewer_id) <= 128 or type(expected_epoch) is not int or expected_epoch < 0:
        raise BrowserLoginError("browser_login_invalid_request")
    stream = connected_viewer_generation(viewer_id)
    if stream is None:
        raise BrowserLoginError("browser_login_screen_not_connected")
    path = runtime.state_dir() / "google-login.json"
    identity = hashlib.sha256(viewer_id.encode()).hexdigest()
    with lease._locked(path):
        current = lease.get()
        previous = _receipt(path)
        if (previous and current.holder == lease.HUMAN and current.viewer_id == viewer_id
                and previous["viewer_hash"] == identity and previous["lease_epoch"] == current.epoch
                and expected_epoch in {previous["requested_epoch"], current.epoch}):
            if previous["phase"] != "opened":
                raise BrowserLoginError("browser_login_outcome_unknown")
            return {"opened": True, "lease": lease.public_view(current)}
        try:
            owned = lease.acquire(viewer_id, expected_epoch=expected_epoch, unless_human=True,
                                  reason="Google browser sign-in")
        except lease.HumanHasControl:
            raise BrowserLoginError("browser_login_control_changed") from None
        receipt = {"viewer_hash": identity, "requested_epoch": expected_epoch,
                   "lease_epoch": owned.epoch, "phase": "attempted"}
        atomic_write_text(path, json.dumps(receipt), mode=0o600)
        current = lease.get()
        if (connected_viewer_generation(viewer_id) != stream or current.holder != lease.HUMAN
                or current.viewer_id != viewer_id or current.epoch != owned.epoch):
            raise BrowserLoginError("browser_login_control_changed")
        try:
            if browser.open_google() is not True:
                raise RuntimeError
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            # A lost acknowledgement cannot be safely retried by opening another tab.
            raise BrowserLoginError("browser_login_outcome_unknown") from None
        current = lease.get()
        if (connected_viewer_generation(viewer_id) != stream or current.holder != lease.HUMAN
                or current.viewer_id != viewer_id or current.epoch != owned.epoch):
            raise BrowserLoginError("browser_login_outcome_unknown")
        receipt["phase"] = "opened"
        atomic_write_text(path, json.dumps(receipt), mode=0o600)
        return {"opened": True, "lease": lease.public_view(lease.get())}
