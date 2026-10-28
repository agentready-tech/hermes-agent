"""One foreground owner of the native backend, gateway and optional mail poller."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path
from urllib.request import urlopen

import psutil

from hermes_runtime.standalone.config import child_environment, initialize

logger = logging.getLogger(__name__)


@contextmanager
def owner_lock(home):
    # Reuse native cross-platform locking, with a distinct slot: backups can
    # still acquire the real home's backup lock while this service runs.
    from hermes_cli.backup import _backup_operation_lock
    with _backup_operation_lock(Path(home) / "autonomous" / "owner", timeout_seconds=0):
        yield


def fingerprint(pid):
    try:
        return {"pid": pid, "created_at": psutil.Process(pid).create_time()}
    except psutil.NoSuchProcess:
        return {"pid": pid, "created_at": None}


def alive(identity):
    try:
        process = psutil.Process(identity["pid"])
        return process.create_time() == identity["created_at"] and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def status(home):
    path = Path(home) / "autonomous" / "status.json"
    if not path.exists():
        return {"ok": False, "reason": "service has not started"}
    value = json.loads(path.read_text())
    value["ok"] = alive(value["owner"]) and set(value["children"]) == set(value["expected"]) and bool(value["children"]) and all(
        alive(c) for c in value["children"].values())
    if value["ok"]:
        try:
            with urlopen(f"http://127.0.0.1:{value['port']}/api/health", timeout=2) as response:
                value["ok"] = bool(json.load(response).get("ok"))
        except (OSError, ValueError):
            value["ok"] = False
    if value["ok"] and "gateway" in value["children"]:
        from gateway.status import read_runtime_status, runtime_status_is_stale, runtime_status_pid_is_live
        gateway = read_runtime_status(Path(home) / "gateway_state.json")
        value["ok"] = bool(gateway and gateway.get("gateway_state") in {"running", "degraded"}
            and gateway.get("pid") == value["children"]["gateway"]["pid"]
            and not runtime_status_is_stale(gateway) and runtime_status_pid_is_live(gateway))
    return value


def reap_abandoned(home):
    """Reclaim only fingerprinted children of a dead foreground owner."""
    path = Path(home) / "autonomous" / "status.json"
    if not path.exists():
        return
    previous = json.loads(path.read_text())
    children = [identity for identity in previous.get("children", {}).values() if alive(identity)]
    if not children:
        return
    if alive(previous["owner"]):
        raise RuntimeError("The previous autonomous owner is still alive")
    processes = [psutil.Process(identity["pid"]) for identity in children]
    for process in processes:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    _, remaining = psutil.wait_procs(processes, timeout=45)
    for process in remaining:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    psutil.wait_procs(remaining, timeout=5)


def park_container_slots():
    """Transfer this container's root slots from s6 to the foreground owner."""
    from hermes_cli.service_manager import _s6_run
    from hermes_constants import get_default_hermes_root, get_hermes_home
    if get_default_hermes_root().resolve() != get_hermes_home().resolve():
        return
    for name in ("gateway-default", "dashboard"):
        path = Path("/run/service") / name
        if path.is_dir():
            _s6_run("s6-svc", "-d", str(path), check=True)
            _s6_run("s6-svwait", "-d", "-t", "15000", str(path), timeout=20, check=True)


class Supervisor:
    def __init__(self, home, specs, *, port, environment=None, restart_delay=2):
        self.home, self.specs, self.port = Path(home), specs, port
        self.environment = environment
        self.restart_delay = restart_delay
        self.stop = threading.Event()
        self.children, self.failures, self.next_start = {}, {}, {}

    def publish(self):
        from utils import atomic_json_write
        atomic_json_write(self.home / "autonomous" / "status.json", {
            "owner": fingerprint(os.getpid()), "port": self.port,
            "children": {k: {**fingerprint(p.pid), "restarts": self.failures.get(k, 0)} for k, p in self.children.items()},
            "expected": list(self.specs),
        }, mode=0o600, fsync_dir=True)

    def tick(self):
        for name, command in self.specs.items():
            process = self.children.get(name)
            if process is not None and process.poll() is not None:
                logger.warning("%s exited with %s; restarting this service", name, process.returncode)
                self.failures[name] = self.failures.get(name, 0) + 1
                self.next_start[name] = time.monotonic() + min(30, self.restart_delay * self.failures[name])
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                del self.children[name]
            if name not in self.children and time.monotonic() >= self.next_start.get(name, 0):
                self.children[name] = subprocess.Popen(command, env=self.environment,
                    stdin=subprocess.DEVNULL, start_new_session=True)
        self.publish()

    def shutdown(self):
        running = [p for p in self.children.values() if p.poll() is None]
        for process in running:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 45
        for process in running:
            try:
                process.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        self.publish()

    def run(self, prepare=None):
        self.home.joinpath("logs").mkdir(exist_ok=True)
        with owner_lock(self.home):
            reap_abandoned(self.home)
            if prepare is not None:
                prepare()
            try:
                while not self.stop.is_set():
                    self.tick()
                    self.stop.wait(.5)
            finally:
                self.shutdown()


def run(home, *, host=None, port=None):
    from hermes_platform.host.facts import os_family
    if os_family() == "win32":
        raise ValueError("Foreground supervision requires Linux/macOS; use the Linux Docker image on Windows")
    config = initialize(home)["autonomous"]
    if not config.get("enabled"):
        raise ValueError("Set autonomous.enabled: true in config.yaml to run the autonomous service")
    host, port = host or config["host"], port or config["port"]
    environment = child_environment(home)
    from hermes_runtime.standalone.service import _argv, _command
    native = _command("hermes_runtime", home=home)
    import hermes_cli
    frontend = "dashboard" if (Path(hermes_cli.__file__).parent / "web_dist" / "index.html").is_file() else "serve"
    specs = {
        "backend": [*native, frontend, "--host", host, "--port", str(port), "--isolated", "--no-open", "--skip-build"],
        "gateway": [*native, "gateway", "run", "--no-supervise", "--external-supervisor"],
    }
    if config.get("gmail", {}).get("enabled"):
        specs["gmail"] = _argv(home, sys.executable, "gmail-worker")
    owner = Supervisor(home, specs, port=port, environment=environment)
    def prepare():
        from hermes_runtime.standalone.config import activate_home
        park_container_slots()
        # Schema/lineage creation has one owner before the native children race
        # to open the same fresh SQLite home on a shared filesystem.
        activate_home(home)
    old = {sig: signal.signal(sig, lambda *_: owner.stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        owner.run(prepare=prepare)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
