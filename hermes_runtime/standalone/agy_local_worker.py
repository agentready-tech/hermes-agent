"""Run one profile's Antigravity CLI in namespaces inside the shared Hermes Pod."""

from contextlib import nullcontext
import ctypes
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from hermes_runtime.standalone.agy_confine import confine
from hermes_runtime.standalone.agy_local_egress import loopback_endpoint, namespace_relay, outbound_relay
from hermes_runtime.standalone.agy_slot import operate
from providers.agy_policy import AgyError, BINARY

SLOT_UID = 65534
LOCAL_CONFIG = Path("/run/agentsee/agy/config.json")
LOCAL_STORE = Path("/run/agentsee/agy-stores")
SOURCE = Path(__file__).resolve().parents[2]
ENTRY = ("import sys,runpy; sys.path.insert(0, sys.argv.pop(1)); "
         "runpy.run_module('hermes_runtime.standalone.agy_local_worker', run_name='__main__')")


def endpoint_addresses(config):
    hosts = json.loads(config.read_bytes())["hosts"]
    if not isinstance(hosts, dict) or not 1 <= len(hosts) <= 32:
        raise AgyError("unavailable")
    for name, value in hosts.items():
        if (not re.fullmatch(r"(?:[a-z0-9-]+\.)+googleapis\.com|accounts\.google\.com", name)
                or not isinstance(value, str)):
            raise AgyError("unavailable")
        address = IPv4Address(value)
        if not address.is_global or address.is_multicast:
            raise AgyError("unavailable")
    return dict(sorted(hosts.items()))


def command(*args):
    return [sys.executable, "-I", "-c", ENTRY, str(SOURCE), *map(str, args)]


def start(action, generation, descriptor, config):
    hosts = endpoint_addresses(config)
    with TemporaryDirectory(prefix="agy-local-", dir="/tmp") as temporary:
        directory = Path(temporary)
        os.chown(directory, SLOT_UID, SLOT_UID)
        listener = directory / "outbound.sock"
        outbound = outbound_relay(listener, tuple(hosts.values()), SLOT_UID) if action == "launch" else nullcontext()
        with outbound:
            child = subprocess.run(
                ["/usr/bin/unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork",
                 "--kill-child", "--propagation", "unchanged",
                 *command("namespace", action, generation, descriptor, directory, config)],
                user=SLOT_UID, group=SLOT_UID, extra_groups=[], pass_fds=(descriptor,), check=False,
            )
            return child.returncode if child.returncode >= 0 else 128 - child.returncode


def bind(source, target, *, readonly=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(source).is_dir():
        target.mkdir(exist_ok=True)
    else:
        target.touch()
    subprocess.run(["/usr/bin/mount", "-n", "--bind", str(source), str(target)], check=True)
    if readonly:
        subprocess.run(["/usr/bin/mount", "-n", "-o", "remount,bind,ro", str(target)], check=True)


def local_namespace(action, generation, descriptor, directory, config):
    try:
        os.fchdir(descriptor)
    finally:
        os.close(descriptor)
    # Keyring helpers need an absolute path and inherit the selected directory.
    # This process's view is outside the CLI's filesystem jail.
    store = Path("/proc/self/cwd")
    if action != "launch":
        return operate(action, generation, auth=store)
    hosts = endpoint_addresses(config)
    cage = directory / "root"

    def launch(home, workdir, bus, argv, environment):
        prepare_root(cage, home, workdir, bus, hosts)
        env = {**environment, "HOME": "/home", "XDG_CONFIG_HOME": "/home/config", "XDG_CACHE_HOME": "/home/cache",
               "XDG_STATE_HOME": "/home/state", "TMPDIR": "/work", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus"}
        return command("cli", cage, *argv), env

    with namespace_relay(directory / "outbound.sock", len(hosts)):
        return operate(action, generation, auth=store, launcher=launch)


def prepare_root(root, home, workdir, bus, hosts):
    for directory in ("/usr/lib", "/lib64", "/etc/ssl/certs", str(Path(BINARY).parent)):
        if Path(directory).is_dir():
            bind(directory, root / directory.removeprefix("/"), readonly=True)
    (root / "lib").symlink_to("usr/lib")
    for name in ("resolv.conf", "nsswitch.conf", "os-release", "passwd", "machine-id"):
        path = root / "etc" / name
        if name == "resolv.conf":
            path.write_text("nameserver 127.0.0.1\n", encoding="ascii")
        elif name == "nsswitch.conf":
            path.write_text("hosts: files\n", encoding="ascii")
        elif Path("/etc", name).is_file():
            bind(Path("/etc", name), path, readonly=True)
    lines = ["127.0.0.1 localhost", *(f"{loopback_endpoint(i)} {host}" for i, host in enumerate(hosts))]
    (root / "etc/hosts").write_text("\n".join(lines) + "\n", encoding="ascii")
    for device, readonly in (("null", False), ("urandom", True)):
        bind(f"/dev/{device}", root / "dev" / device, readonly=readonly)
    bind(home, root / "home")
    bind(workdir, root / "work")
    bind(bus.removeprefix("unix:path="), root / "run/bus")
    # The pinned Go CLI looks up its own path when it starts an ACP worker.
    # Expose that path without mounting the Hermes process table.
    (root / "proc/self").mkdir(parents=True)
    (root / "proc/self/exe").symlink_to(BINARY)


def start_cli(root, argv):
    os.chroot(root)
    os.chdir("/work")
    confine("/home", "/work")
    libc = ctypes.CDLL(None, use_errno=True)
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)  # Linux capability API v3, current task.
    capabilities = (ctypes.c_uint32 * 6)()
    # Root in a user namespace must not regain capabilities on exec.
    if libc.prctl(28, 3, 0, 0, 0) or libc.syscall(126, ctypes.byref(header), ctypes.byref(capabilities)):
        raise AgyError("unavailable")
    os.execv(BINARY, argv)


def main():
    mode, *args = sys.argv[1:]
    try:
        if sys.platform != "linux" or os.uname().machine != "x86_64":
            raise AgyError("unavailable")
        if mode == "start":
            action, generation, descriptor, config = args
            return start(action, generation, int(descriptor), Path(config))
        if mode == "namespace":
            action, generation, descriptor, directory, config = args
            return local_namespace(action, generation, int(descriptor), Path(directory), Path(config))
        if mode == "cli":
            root, *argv = args
            start_cli(root, argv)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return 125
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
