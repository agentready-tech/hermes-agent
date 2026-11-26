"""Run the managed browser CLI and the agent's Python in its Kubernetes sandbox."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from tools.environments.base import EnvironmentConnectionError
from tools.environments.kubernetes import KubernetesEnvironment
from tools.environments.streams import client_environment

_PAYLOAD = Path("/usr/local/share/agentsee/sandbox-browser.tar.gz")
_STATE = "/tmp/agentsee-bh"  # no-tmp: ok — short private directory in the sandbox Pod
_PYTHON = "/usr/bin/python3"
_INSTALL = """
from pathlib import Path
import fcntl, hashlib, io, os, stat, sys, tarfile, tempfile

destination = Path(sys.argv[1])
digest = sys.argv[2]
parent = destination.parent
parent.mkdir(mode=0o700, exist_ok=True)
info = parent.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit(1)
with (parent / "install.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if (destination / "browser_harness/run.py").is_file():
        raise SystemExit(0)
    if sys.argv[3] == "check":
        raise SystemExit(3)
    data = sys.stdin.buffer.read(int(sys.argv[4]) + 1)
    if len(data) != int(sys.argv[4]) or hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit(1)
    with tempfile.TemporaryDirectory(dir=parent) as staging:
        extracted = Path(staging) / "payload"
        extracted.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(extracted, filter="data")
        extracted.rename(destination)
"""
_RUN = """
import os, runpy, sys
from pathlib import Path
workspace = Path(os.environ["BH_AGENT_WORKSPACE"])
workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chdir(workspace)
sys.path.insert(0, os.environ["PYTHONPATH"])
sys.argv[1:] = []
runpy.run_module("browser_harness.run", run_name="__main__")
"""


@lru_cache(maxsize=1)
def _bundle():
    data = _PAYLOAD.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


class BrowserKubernetesExec:
    state = _STATE

    def __init__(self, environment: KubernetesEnvironment, cdp: str, task: str):
        if not isinstance(environment, KubernetesEnvironment):
            raise EnvironmentConnectionError("The employee browser sandbox is unavailable")
        parts = urlsplit(cdp)
        if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname:
            raise EnvironmentConnectionError("The employee browser sandbox endpoint is unavailable")
        self._environment = environment
        self.cdp = cdp
        self._uid = environment.kubernetes_identity["uid"]
        self._payload, self._digest = _bundle()
        self._site = f"{self.state}/site-{self._digest[:16]}"
        self.workspace = "/workspace/browser-exec/" + hashlib.sha256(task.encode()).hexdigest()[:20]
        ready = (self._uid, self._digest)
        if getattr(environment, "_browser_payload_ready", None) != ready:
            self._install()
            environment._browser_payload_ready = ready

    def _argv(self, args, *, interactive):
        return [*self._environment.exec_prefix(interactive=interactive, require_current=True, expected_uid=self._uid),
                *args]

    def _helper(self, code, args=(), *, data=None, timeout=15):
        return subprocess.run(self._argv([_PYTHON, "-I", "-B", "-c", code, *args], interactive=data is not None),
                              input=data, capture_output=True, check=False, timeout=timeout,
                              env=client_environment())

    def _install(self):
        args = [self._site, self._digest]
        result = self._helper(_INSTALL, [*args, "check"])
        if result.returncode == 3:
            result = self._helper(_INSTALL, [*args, "install", str(len(self._payload))],
                                  data=self._payload, timeout=60)
        if result.returncode:
            raise EnvironmentConnectionError("The browser CLI could not be prepared inside the employee sandbox")

    def driver_processes(self) -> dict[int, float | None]:
        result = self._helper(
            "import json, sys; sys.path.insert(0, sys.argv[1]); import psutil; "
            "print(json.dumps({p.pid: p.info['create_time'] "
            "for p in psutil.process_iter(['create_time'], ad_value=None)}))", [self._site])
        if result.returncode:
            raise EnvironmentConnectionError("Could not identify the employee sandbox browser driver")
        return {int(pid): start for pid, start in json.loads(result.stdout).items()}

    def run(self, _cmd, code, env, timeout):
        from tools.browser_use_cli import _run_cli_killing_process_group, _set_cdp_env

        remote = {
            "HOME": "/root",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": self._site,
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANONYMIZED_TELEMETRY": "false",
            "BU_NAME": env.get("BU_NAME", "default"),
            "BH_HOME": f"{self.state}/state",
            "BH_TMP_DIR": self.workspace,
            "BH_TMP_DIR_SHARED": "1",
            "BH_AGENT_WORKSPACE": self.workspace,
        }
        _set_cdp_env(remote, self.cdp)
        # The command's own deadline also applies if the exec client disconnects.
        command = ["/usr/bin/env", "-i", *(f"{key}={value}" for key, value in remote.items()),
                   "/usr/bin/timeout", "--signal=KILL", str(timeout), _PYTHON, "-I", "-B", "-c", _RUN]
        return _run_cli_killing_process_group(
            self._argv(command, interactive=True), code, client_environment(), timeout + 8)
