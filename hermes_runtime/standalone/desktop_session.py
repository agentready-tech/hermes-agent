"""Bind a Linux service to the current user's Xfce login, never a guessed or previous-boot display."""

import json
import logging
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)
GUI_KEYS = frozenset({"DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE", "XDG_SESSION_ID", "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "AT_SPI_BUS_ADDRESS",
    "WAYLAND_DISPLAY", "XDG_CONFIG_HOME"})


def snapshot_path(home):
    return Path(home) / "autonomous" / "desktop-session.json"


def _read_snapshot(home):
    path = snapshot_path(home)
    try:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_size > 65536):
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("uid") != os.getuid() or value.get("boot_time") != psutil.boot_time():
        return None
    environment = value.get("environment")
    if (not isinstance(environment, dict) or not _local_x11(environment)
            or any(k not in GUI_KEYS or not isinstance(v, str) or "\0" in v for k, v in environment.items())):
        return None
    return value


def _local_x11(environment):
    display = environment.get("DISPLAY", "")
    return isinstance(display, str) and re.fullmatch(r":\d+(?:\.\d+)?", display) is not None


def _xfce_environment(environment):
    desktop = ":".join(str(environment.get(k, "")) for k in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION"))
    return any(name.lower() in {"xfce", "xubuntu"} for name in desktop.split(":"))


def xfce_sessions():
    candidates = []
    for process in psutil.process_iter(["name", "uids"]):
        try:
            if process.info["name"] != "xfce4-session" or process.info["uids"].real != os.getuid():
                continue
            desktop = process.environ()
            if _local_x11(desktop):
                desktop.setdefault("XDG_CURRENT_DESKTOP", "XFCE")
                candidates.append(desktop)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return candidates


def discover_session(environment=None):
    """An SSH installer may adopt exactly one Xfce session belonging to its own Unix account."""
    environment = dict(os.environ if environment is None else environment)
    if _local_x11(environment) and _xfce_environment(environment):
        return environment
    candidates = {env["DISPLAY"]: env for env in xfce_sessions()}
    if len(candidates) > 1:
        raise ValueError("Several Xfce sessions belong to this account; run install-service from the desired desktop")
    return next(iter(candidates.values()), environment)


def _notify_service(home, changed, environment):
    from hermes_runtime.standalone.service import service_name
    from tools.process_registry import systemd_user_bus_env

    name = service_name(home)
    bus = systemd_user_bus_env(dict(environment))
    enabled = subprocess.run(["systemctl", "--user", "is-enabled", name], env=bus,
        capture_output=True, text=True, check=False, timeout=15)
    if enabled.returncode or enabled.stdout.strip() not in {"enabled", "enabled-runtime"}:
        return False
    subprocess.run(["systemctl", "--user", "--no-block", "restart" if changed else "start", name],
        env=bus, check=True, timeout=30)
    return True


def capture_session(home, environment=None, *, notify=True):
    from utils import atomic_json_write

    environment = dict(os.environ if environment is None else environment)
    if not _local_x11(environment) or not _xfce_environment(environment):
        return {"captured": False, "reason": "waiting for this user's Xfce X11 login"}
    gui = {k: v for k, v in environment.items() if k in GUI_KEYS and k != "WAYLAND_DISPLAY" and v}
    if any(not isinstance(v, str) or "\0" in v or len(v) > 4096 for v in gui.values()):
        raise ValueError("Invalid graphical session environment")
    gui["XDG_SESSION_TYPE"] = "x11"
    value = {"uid": os.getuid(), "boot_time": psutil.boot_time(), "environment": gui}
    previous = _read_snapshot(home) or {}
    changed = any(previous.get(k) != v for k, v in value.items()) or not previous.get("service_notified")
    value["service_notified"] = False
    atomic_json_write(snapshot_path(home), value, mode=0o600, fsync_dir=True)
    notified = _notify_service(home, changed, environment) if notify else False
    if notified:
        atomic_json_write(snapshot_path(home), {**value, "service_notified": True}, mode=0o600, fsync_dir=True)
    return {"captured": True, "display": gui["DISPLAY"], "service_notified": notified}


def session_environment(home, environment=None):
    environment = dict(os.environ if environment is None else environment)
    if snapshot := _read_snapshot(home):
        environment = {k: v for k, v in environment.items() if k not in GUI_KEYS}
        environment.update(snapshot["environment"])
    return environment


def run_service(home):
    # The user manager may retain a display imported by an old SSH/RDP login.
    # Only a current-boot hook or a live Xfce process can supply the binding.
    environment = {k: v for k, v in os.environ.items() if k not in GUI_KEYS or k == "XDG_RUNTIME_DIR"}
    from hermes_runtime.standalone.config import settings
    if settings(home).get("rdp"):
        from hermes_runtime.standalone.rdp_session import wait_for_desktop
        wait_for_desktop(home)
    if not _read_snapshot(home):
        capture_session(home, discover_session({}), notify=False)
    environment = session_environment(home, environment)
    if environment.get("DISPLAY"):
        logger.info("Autonomous service using Xfce display %s", environment["DISPLAY"])
    else:
        logger.info("Autonomous service started; Xfce login hook will attach the desktop when available")
    os.execvpe(sys.executable, [sys.executable, "-m", "hermes_runtime.standalone.cli", "--home", str(home), "run"], environment)
