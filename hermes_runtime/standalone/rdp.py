"""Install one boot-started xrdp/Xfce session for the service's Unix account."""

import configparser
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from hermes_platform.resolver import locate_command


def command(name):
    value = locate_command(name).command
    if not value:
        raise ValueError(f"RDP setup requires the command {name}")
    return list(value)


def unit_name(uid):
    return f"hermes-xfce-rdp-{uid}.service"


def configure_ini(text, section, changes):
    """Change only selected keys, preserving the vendor's comments and other sections."""
    lines, result, active, found = text.splitlines(keepends=True), [], False, False
    pending = {key.lower(): (key, str(value)) for key, value in changes.items()}
    for line in lines:
        header = re.match(r"^\s*\[([^]]+)\]", line)
        if header:
            if active:
                result.extend(f"{key}={value}\n" for key, value in pending.values())
                pending.clear()
            active = header[1].lower() == section.lower()
            found = found or active
        key = re.match(r"^\s*([^;#\s=]+)\s*=", line) if active else None
        if key and key[1].lower() in {k.lower() for k in changes}:
            canonical = next(k for k in changes if k.lower() == key[1].lower())
            line = f"{canonical}={changes[canonical]}\n"
            pending.pop(key[1].lower(), None)
        result.append(line if line.endswith("\n") else line + "\n")
    if not found:
        result.append(f"\n[{section}]\n")
    result.extend(f"{key}={value}\n" for key, value in pending.values())
    return "".join(result)


def render_unit(home, interpreter, user):
    from hermes_runtime.standalone.service import _argv, _systemd_quote

    launch = " ".join(_systemd_quote(v) for v in _argv(home, interpreter, "rdp-start"))
    config = str(Path(os.environ.get("XDG_CONFIG_HOME") or Path(user.pw_dir) / ".config").resolve())
    setting = _systemd_quote("XDG_CONFIG_HOME=" + config, exec_line=False)
    return ("# Managed by hermes-autonomous install-service --rdp\n"
        "[Unit]\nDescription=Shared Hermes Xfce RDP desktop\n"
        "Requires=xrdp.service xrdp-sesman.service\nAfter=xrdp.service xrdp-sesman.service\n\n"
        f"[Service]\nType=oneshot\nUser={user.pw_name}\nEnvironment={setting}\n"
        f"ExecStart={launch}\nRemainAfterExit=yes\nRestart=on-failure\nRestartSec=5\nTimeoutStartSec=120\n\n"
        "[Install]\nWantedBy=multi-user.target\n")


def preflight(home):
    import pwd
    from hermes_runtime.standalone.config import read_config
    from hermes_runtime.standalone.rdp_session import check_sessions, xfce_sessions
    from hermes_runtime.standalone.rdp_packages import ensure

    config = read_config(home)
    for section in ("terminal", "browser", "bot_desktop"):
        if not isinstance(config.get(section, {}), dict):
            raise ValueError(f"{section} must be a mapping in config.yaml for RDP setup")
    user = pwd.getpwuid(os.getuid())
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*\$?", user.pw_name):
        raise ValueError("Unsupported Unix account name for the RDP service")
    for name in ("startxfce4", "xfce4-session", "xfconf-query"):
        command(name)
    check_sessions(xfce_sessions())
    if os.geteuid() != 0:
        command("sudo")
    ensure()
    for name in ("xrdp-sesrun", "xdpyinfo"):
        command(name)
    version = subprocess.run([*command("xrdp"), "--version"], capture_output=True, text=True, check=True, timeout=15)
    match = re.search(r"\bxrdp\s+(\d+)\.(\d+)", version.stdout + version.stderr)
    if not match or tuple(map(int, match.groups())) < (0, 10):
        raise ValueError("Boot-started RDP requires xrdp >= 0.10 (passwordless local xrdp-sesrun)")
    ini = configparser.ConfigParser(interpolation=None, strict=False)
    ini.read_string(Path("/etc/xrdp/xrdp.ini").read_text())
    if "Xorg" not in ini or ini["Xorg"].get("code") != "20":
        raise ValueError("RDP setup requires the native [Xorg] backend in /etc/xrdp/xrdp.ini and xorgxrdp")
    Path("/etc/xrdp/sesman.ini").read_text()
    return user


def root_run(*args, timeout=90, **kwargs):
    prefix = [] if os.geteuid() == 0 else [*command("sudo"), "--"]
    return subprocess.run([*prefix, *args], check=True, timeout=timeout, **kwargs)


def write_system_file(path, text, *, backup=False):
    """Copy to a same-filesystem temporary file, then atomically replace the target."""
    previous = path.stat() if path.exists() else None
    if previous and path.read_text() == text:
        return
    saved = Path(str(path) + ".hermes-before-rdp")
    if previous and backup and not saved.exists():
        root_run("cp", "--preserve=all", "--", str(path), str(saved))
    mode, uid, gid = (previous.st_mode & 0o777, previous.st_uid, previous.st_gid) if previous else (0o644, 0, 0)
    destination = path.with_name(f".{path.name}.hermes-{uuid.uuid4().hex}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as source:
        source.write(text)
        source.flush()
        root_run("install", "-o", str(uid), "-g", str(gid), "-m", f"{mode:o}", source.name, str(destination))
        try:
            root_run("mv", "-f", "--", str(destination), str(path))
        finally:
            root_run("rm", "-f", "--", str(destination))


def remember(home):
    from hermes_yaml import roundtrip_yaml
    from utils import atomic_write_text

    path = Path(home) / "config.yaml"
    backup = path.with_suffix(".yaml.hermes-before-rdp")
    if path.exists() and not backup.exists():
        atomic_write_text(backup, path.read_text(), mode=0o600)
    yaml = roundtrip_yaml()
    config = yaml.load(path.read_text()) if path.exists() else {}
    config = {} if config is None else config
    config.setdefault("autonomous", {})["rdp"] = True
    config.setdefault("terminal", {})["backend"] = "local"
    config.setdefault("browser", {}).update({"headed": True, "cloud_provider": "local", "engine": "chrome", "cdp_url": ""})
    config.setdefault("bot_desktop", {})["auto_start"] = False
    output = io.StringIO()
    yaml.dump(config, output)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, output.getvalue(), mode=0o600)


def prepare_desktop(user):
    from utils import atomic_write_text

    folder = Path(os.environ.get("XDG_CONFIG_HOME") or Path(user.pw_dir) / ".config") / "autostart"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("xfce4-screensaver", "light-locker", "xfce4-power-manager"):
        path = folder / f"{name}.desktop"
        value = f"[Desktop Entry]\nType=Application\nName={name}\nHidden=true\n"
        if path.exists() and path.read_text() != value:
            backup = path.with_suffix(".desktop.hermes-before-rdp")
            if not backup.exists():
                atomic_write_text(backup, path.read_text(), mode=0o600)
        atomic_write_text(path, value, mode=0o600)


def prepare_tls(config_path, standard_key):
    """Allow the unprivileged daemon to read Debian's default TLS certificate key."""
    import grp
    import pwd

    ini = configparser.ConfigParser(interpolation=None, strict=False)
    ini.read_string(config_path.read_text())
    runtime_user = ini["Globals"].get("runtime_user", "root").strip()
    if runtime_user in ("", "root"):
        return False
    key = Path(ini["Globals"].get("key_file") or config_path.with_name("key.pem"))
    if key.resolve() != standard_key.resolve():
        return False
    user, group = pwd.getpwnam(runtime_user), grp.getgrnam("ssl-cert")
    if group.gr_gid in os.getgrouplist(user.pw_name, user.pw_gid):
        return False
    root_run(*command("usermod"), "-a", "-G", group.gr_name, "--", user.pw_name)
    return True


def install(home, user):
    from utils import atomic_write_text

    folder = Path("/etc/systemd/system")
    unit = folder / unit_name(user.pw_uid)
    sesman = Path("/etc/xrdp/sesman.ini")
    updated = configure_ini(sesman.read_text(), "Sessions", {
        "Policy": "Default", "KillDisconnected": "false", "DisconnectedTimeLimit": "0", "IdleTimeLimit": "0"})
    updated = configure_ini(updated, "Security", {"AllowAlternateShell": "true"})
    write_system_file(sesman, updated, backup=True)
    xrdp = Path("/etc/xrdp/xrdp.ini")
    write_system_file(xrdp, configure_ini(xrdp.read_text(), "Globals", {"autorun": "Xorg"}), backup=True)
    tls_group_added = prepare_tls(xrdp, Path("/etc/ssl/private/ssl-cert-snakeoil.key"))
    write_system_file(unit, render_unit(home, sys.executable, user))
    prepare_desktop(user)
    config = str(Path(os.environ.get("XDG_CONFIG_HOME") or Path(user.pw_dir) / ".config").resolve())
    launcher = ("#!/bin/sh\nexport XDG_CURRENT_DESKTOP=XFCE\nexport DESKTOP_SESSION=xfce\n"
        f"export XDG_CONFIG_HOME={shlex.quote(config)}\nexec {shlex.quote(command('startxfce4')[0])}\n")
    atomic_write_text(Path(home) / "autonomous" / "rdp-xfce.sh", launcher, mode=0o700)
    remember(home)
    return {"unit": str(unit), "user": user.pw_name, "backend": "Xorg", "reconnect": "same user, Xorg",
            "tls_group_added": tls_group_added}


def enable(user, *, restart_rdp=False):
    root_run("systemctl", "daemon-reload")
    root_run("systemctl", "enable", "--now", "xrdp.service")
    if restart_rdp:
        # Apply newly granted supplementary groups to an already-running daemon.
        root_run("systemctl", "restart", "xrdp.service")
    root_run("systemctl", "reload", "xrdp-sesman.service")
    root_run("systemctl", "enable", "--now", unit_name(user.pw_uid))
