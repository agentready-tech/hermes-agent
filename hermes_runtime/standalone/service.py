"""Linux user service and Xfce login hook for one persistent Hermes home."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path


def service_name(home):
    return "hermes-autonomous-" + hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:12] + ".service"


def _systemd_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$").replace("\n", "\\n") + '"'


def _argv(home, interpreter, action):
    return [interpreter, "-m", "hermes_runtime.standalone.cli", "--home", str(Path(home).resolve()), action]


def render_service(home, interpreter):
    command = " ".join(_systemd_quote(v) for v in _argv(home, interpreter, "service-run"))
    return ("[Unit]\nDescription=Autonomous Hermes\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\n" + f"ExecStart={command}\n" +
        "Restart=always\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=60\n\n"
        "[Install]\nWantedBy=default.target\n")


def _desktop_quote(value):
    # Exec has both Desktop Entry string escaping and argument escaping; it is never a shell command.
    value = str(value).replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    value = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    return '"' + value + '"'


def render_autostart(home, interpreter):
    command = " ".join(_desktop_quote(v) for v in _argv(home, interpreter, "desktop-session"))
    return ("[Desktop Entry]\nType=Application\nName=Hermes autonomous session binding\n"
        f"Exec={command}\nTerminal=false\nOnlyShowIn=XFCE;\nX-GNOME-Autostart-enabled=true\n")


def install_service(home, enable):
    from hermes_platform.host.facts import os_family
    from tools.process_registry import systemd_user_bus_env
    from utils import atomic_write_text
    from hermes_runtime.standalone.desktop_session import capture_session, discover_session

    if os_family() != "linux":
        raise ValueError("install-service requires Linux systemd; use Docker up with automatic restart on this host")
    desktop = discover_session()
    name = service_name(home)
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    folder = config / "systemd" / "user"
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(folder / name, render_service(home, sys.executable), mode=0o600)
    autostart = config / "autostart" / (name.removesuffix(".service") + ".desktop")
    autostart.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(autostart, render_autostart(home, sys.executable), mode=0o600)
    session = capture_session(home, desktop, notify=False)
    if enable:
        environment = systemd_user_bus_env()
        subprocess.run(["loginctl", "enable-linger"], env=environment, check=True, timeout=30)
        subprocess.run(["systemctl", "--user", "daemon-reload"], env=environment, check=True, timeout=30)
        subprocess.run(["systemctl", "--user", "enable", name], env=environment, check=True, timeout=30)
        # Reinstalling refreshes the running owner's display binding and interpreter too.
        subprocess.run(["systemctl", "--user", "restart", name], env=environment, check=True, timeout=90)
    return {"unit": str(folder / name), "enabled": enable, "autostart": str(autostart), "desktop_session": session,
            "boot_requirement": "The VM must start this user's existing Xfce session at boot and keep it alive without a viewer (e.g. LightDM autologin)."}
