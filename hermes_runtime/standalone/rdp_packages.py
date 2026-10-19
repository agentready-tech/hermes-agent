"""Provision the native RDP stack on an Ubuntu/Debian Xfce host."""

import re
import subprocess
import sys
from pathlib import Path

from hermes_platform.resolver import locate_command
from hermes_runtime.standalone.rdp import command, root_run

PACKAGES = ("xrdp", "xorgxrdp")
DESKTOP = ("xserver-xorg-core", "xauth", "x11-utils", "x11-xserver-utils", "dbus", "dbus-x11")


def compatible(values):
    """Both halves must be from the 0.10 generation or later, including epoch versions."""
    def recent(value):
        match = re.match(r"(?:\d+:)?(\d+)\.(\d+)", value or "")
        return bool(match and tuple(map(int, match.groups())) >= (0, 10))
    return all(recent(values.get(name)) for name in PACKAGES)


def versions():
    result = subprocess.run([*command("dpkg-query"), "-W", "-f=${Package}\t${Status}\t${Version}\n", *PACKAGES],
        capture_output=True, text=True, timeout=15, check=False)
    rows = (line.split("\t") for line in result.stdout.splitlines())
    return {row[0]: row[2] for row in rows if len(row) == 3 and row[1] == "install ok installed"}


def candidates():
    result = {}
    for name in PACKAGES:
        value = subprocess.run([*command("env"), "LC_ALL=C", *command("apt-cache"), "policy", name],
            capture_output=True, text=True, check=True, timeout=30)
        match = re.search(r"^\s*Candidate:\s*(\S+)", value.stdout, re.MULTILINE)
        result[name] = match[1] if match else ""
    return result


def apt(*args):
    root_run("env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l", *command("apt-get"),
        "-o", "DPkg::Lock::Timeout=120", "-o", "Dpkg::Options::=--force-confold",
        "--no-install-recommends", "--no-remove", "-y", *args, timeout=1800, stdout=sys.stderr)


def back_up_config():
    for name in ("xrdp.ini", "sesman.ini"):
        path = Path("/etc/xrdp") / name
        backup = path.with_name(name + ".hermes-before-rdp")
        if path.exists() and not backup.exists():
            root_run("cp", "--preserve=all", "--", str(path), str(backup))


def build_backport():
    from hermes_runtime.standalone.rdp_backport import install

    install()


def ensure():
    """Never replace a compatible stack; backport when the OS only offers xrdp 0.9."""
    command("apt-get")
    installed = versions()
    missing_desktop = any(not locate_command(name).command for name in ("xdpyinfo", "xauth", "dbus-launch", "dbus-daemon"))
    if compatible(installed) and not missing_desktop:
        return installed
    print("Installing the shared Xfce RDP dependencies...", file=sys.stderr, flush=True)
    back_up_config()
    apt("update")
    if compatible(installed) or compatible(candidates()):
        apt("install", *DESKTOP, *PACKAGES)
    else:
        apt("install", *DESKTOP, "build-essential", "dpkg-dev", "fakeroot", "ca-certificates")
        build_backport()
    installed = versions()
    if not compatible(installed):
        raise RuntimeError("RDP package installation did not provide compatible xrdp and xorgxrdp >= 0.10")
    return installed
