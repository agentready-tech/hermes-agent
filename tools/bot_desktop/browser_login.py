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


def _schedule_intent(viewer_id, epoch):
    from tui_gateway.transport import current_transport
    from hermes_cli.web_routers.display import _expire_disconnected_viewer
    from hermes_constants import get_hermes_home
    transport=current_transport();loop=getattr(transport,"_loop",None)
    if loop is None or loop.is_closed() or getattr(transport,"closed",False):
        raise BrowserLoginError("browser_window_rpc_unavailable")
    loop.call_soon_threadsafe(_expire_disconnected_viewer,str(get_hermes_home()),viewer_id,epoch)


def _rpc_live():
    from tui_gateway.transport import current_transport
    transport=current_transport()
    return transport is not None and not getattr(transport,"closed",False)


def open_google(viewer_id, expected_epoch):
    from tools.bot_desktop import browser_window
    from utils import atomic_write_text

    if not isinstance(viewer_id,str) or not 1<=len(viewer_id)<=128 or type(expected_epoch) is not int or expected_epoch<0:
        raise BrowserLoginError("browser_login_invalid_request")
    browser_window.intent(viewer_id)
    path=runtime.state_dir()/"google-login.json"
    identity=hashlib.sha256(viewer_id.encode()).hexdigest()
    with lease._locked(path):
        current=lease.get();previous=_receipt(path)
        if previous and previous["viewer_hash"]==identity:
            if previous["phase"]!="opened":raise BrowserLoginError("browser_login_outcome_unknown")
            if (current.holder!=lease.HUMAN or current.viewer_id!=viewer_id
                    or previous["lease_epoch"]!=current.epoch
                    or expected_epoch not in {previous["requested_epoch"],current.epoch}):
                raise BrowserLoginError("browser_login_control_changed")
            return {"opened":True,**browser_window.ticket(viewer_id,current.epoch)}
        if not _rpc_live():raise BrowserLoginError("browser_window_rpc_unavailable")
        try:
            owned=lease.acquire(viewer_id,expected_epoch=expected_epoch,unless_human=True,reason="Google browser sign-in")
        except lease.HumanHasControl:
            raise BrowserLoginError("browser_login_control_changed") from None
        external_started=False
        try:
            _schedule_intent(viewer_id,owned.epoch)
            receipt={"viewer_hash":identity,"requested_epoch":expected_epoch,"lease_epoch":owned.epoch,"phase":"attempted"}
            atomic_write_text(path,json.dumps(receipt),mode=0o600)
            if not _rpc_live():raise BrowserLoginError("browser_login_control_changed")
            runtime.start()  # The own existing X display, never a second browser/profile or a Desktop stream.
            external_started=True
            if browser_window.open_google() is not True:raise BrowserLoginError("browser_login_outcome_unknown")
            browser_window.qualify(viewer_id,owned.epoch)
            current=lease.get()
            if (not _rpc_live() or current.holder!=lease.HUMAN
                    or current.viewer_id!=viewer_id or current.epoch!=owned.epoch):
                raise BrowserLoginError("browser_login_control_changed")
            reply=browser_window.ticket(viewer_id,owned.epoch)
            receipt["phase"]="opened";atomic_write_text(path,json.dumps(receipt),mode=0o600)
            return {"opened":True,**reply}
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
            try:browser_window.restore(viewer_id)
            finally:lease.release(viewer_id,expected_epoch=owned.epoch)
            if isinstance(error,(BrowserLoginError,browser_window.WindowUnavailable)):raise
            raise BrowserLoginError("browser_login_outcome_unknown" if external_started else "browser_login_unavailable") from None
