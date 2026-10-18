"""Start or reuse a local xrdp session and bind Hermes before any viewer connects."""

import os
import subprocess
import threading
import time
from pathlib import Path

from hermes_runtime.standalone.desktop_session import capture_session, xfce_sessions
from hermes_runtime.standalone.rdp import command


def check_sessions(sessions):
    # xrdp 0.10 uses its session process PID here, rather than a boolean "1".
    if any(not env.get("XRDP_SESSION") for env in sessions):
        raise ValueError("This account already has an Xfce console session; --rdp will not create a second desktop")
    if len(sessions) > 1:
        raise ValueError("Several Xfce RDP sessions belong to this account; keep one before enabling RDP autostart")
    return sessions[0] if sessions else None


def wait_for_desktop(home, *, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        environment = check_sessions(xfce_sessions())
        if environment:
            probe = subprocess.run([*command("xdpyinfo")], env={**os.environ, **environment},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
            if probe.returncode == 0:
                capture_session(home, environment, notify=False)
                return environment
        if time.monotonic() >= deadline:
            raise RuntimeError("The shared Xfce RDP desktop is not ready; check the hermes-xfce-rdp system service")
        threading.Event().wait(.25)


def start(home):
    from hermes_cli.backup import _backup_operation_lock

    # System units are per Unix account, and a manual retry shares their lock.
    with _backup_operation_lock(Path.home() / ".cache" / "hermes-rdp" / "start", timeout_seconds=60):
        existing = check_sessions(xfce_sessions())
        if not existing:
            launcher = Path(home) / "autonomous" / "rdp-xfce.sh"
            if not launcher.is_file():
                raise ValueError("Run install-service --rdp before starting the shared desktop")
            subprocess.run([*command("xrdp-sesrun"), "-t", "Xorg", "-g", "1920x1080", "-b", "32",
                "-S", str(launcher)], check=True, timeout=60)
        environment = wait_for_desktop(home)
    return {"display": environment["DISPLAY"], "reused": bool(existing)}
