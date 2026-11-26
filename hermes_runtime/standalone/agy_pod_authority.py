"""Profile-bound access to the two Antigravity slots created by provisioner."""

from contextlib import contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess

from hermes_constants import get_default_hermes_root, named_profile_is_live
from hermes_platform.resolver import locate_command
from hermes_runtime.standalone.agy_pod_process import GENERATION, request_frame
from hermes_runtime.standalone.agy_runtime import LOGIN_COMMAND, StagedStore
from providers.agy_policy import AgyError
from tools.environments.base import EnvironmentConnectionError
from tools.environments.kubernetes_lifecycle import SandboxLifecycle
from utils import atomic_json_write

SLOTS = ("a", "b")
ENVIRONMENT = "agentsee.io/provisioning-environment"
JOB = "agentsee.io/provisioning-job"
SA_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
POD_ENTRY = (
    "import runpy, sys; sys.path.insert(0, '/opt/hermes'); "
    "runpy.run_module('hermes_runtime.standalone.agy_pod_process', run_name='__main__')"
)
IDLE_SECONDS = 60


def capture(argv):
    try:
        response = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise AgyError("unavailable") from None
    return response.stdout


def owned_profile(home):
    profile = Path(home)
    match = re.fullmatch(r"env_([a-f0-9]{32})", profile.name)
    if (not match or profile.parent != get_default_hermes_root() / "profiles"
            or profile.resolve(strict=True) != profile or not named_profile_is_live(profile)):
        raise AgyError("unavailable")
    marker = profile / "owner.json"
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AgyError("unavailable")
    owner = json.loads(marker.read_text())
    job = owner.get("jobId", "")
    if owner.get("environmentId") != profile.name or not re.fullmatch(r"job_[a-f0-9]{32}", job):
        raise AgyError("unavailable")
    return profile, f"agentsee-a-{match[1]}", job


def read_slots(prefix, stage, job):
    data = json.loads(capture([*prefix, "get", "statefulset", "agy-a", "agy-b", "-o", "json"]))
    slots = {}
    for resource in data["items"]:
        metadata = resource["metadata"]
        labels = metadata["labels"]
        slot = metadata["name"].removeprefix("agy-")
        pod = resource["spec"]["template"]["spec"]
        container, = pod["containers"]
        image = container["image"]
        if (slot not in SLOTS or labels.get(ENVIRONMENT) != stage or labels.get(JOB) != job
                or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image)):
            raise AgyError("unavailable")
        slots[slot] = (metadata["uid"], image)
    if set(slots) != set(SLOTS) or slots["a"][1] != slots["b"][1]:
        raise AgyError("unavailable")
    return tuple(slots[slot] for slot in SLOTS)


def authority_for(home):
    try:
        stage = {"agentsee-provisioning-test": "test", "agentsee-runtime": "production"}[
            SA_NAMESPACE.read_text().strip()]
        profile, namespace, job = owned_profile(home)
        executable, *_ = locate_command("kubectl").command
        prefix = [executable, "--namespace", namespace]
        return pod_authority(profile, namespace, executable, read_slots(prefix, stage, job), acp=stage == "test")
    except (OSError, TypeError, ValueError, KeyError):
        raise AgyError("unavailable") from None


@lru_cache(maxsize=128)
def pod_authority(home, namespace, kubectl, slots, *, acp=False):
    return PodAuthority(home, namespace, kubectl, slots, acp=acp)


def stop_profile_agy_if_idle():
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    active = [slot for slot in SLOTS if (home / f"kubernetes-agy-{slot}" / "activity").is_file()]
    if active:
        authority = authority_for(home)
        for slot in active:
            authority.lifecycle(slot).stop_if_idle()


class PodAuthority:
    def __init__(self, home, namespace, kubectl, slots, *, acp=False):
        self.acp = acp
        self.home, self.namespace, self.kubectl = home, namespace, kubectl
        self.slots = dict(zip(SLOTS, slots))
        self.root = home.parent.parent / "agy-bindings" / home.name
        self.binding = self.root / "binding.json"

    @contextmanager
    def locked(self, home):
        import fcntl

        if home != self.home:
            raise AgyError("unavailable")
        for directory in (self.root.parent, self.root):
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise AgyError("unavailable")
        fd = os.open(self.root / "binding.lock", os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "rb") as lock:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise AgyError("unavailable")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.load()

    def load(self):
        if self.binding.is_symlink():
            raise AgyError("unavailable")
        try:
            state = json.loads(self.binding.read_text()) if self.binding.exists() else {}
        except (ValueError, OSError):
            raise AgyError("unavailable") from None
        if not isinstance(state, dict):
            raise AgyError("unavailable")
        return state

    def save(self, state):
        atomic_json_write(self.binding, state, mode=0o600)

    def stage(self, home):
        with self.locked(home) as state:
            current = self.current(state.get("active"))
            slot = "b" if current and current["slot"] == "a" else "a"
            generation = secrets.token_hex(16)
            uid, image = self.slots[slot]
            self.control(slot, "prepare", generation)
            state["staged"] = {"slot": slot, "generation": generation, "uid": uid, "image": image}
            self.save(state)
            return StagedStore(generation, f"{self.namespace}/agy-{slot}", uid)

    def current(self, entry):
        if not isinstance(entry, dict):
            return None
        slot, generation = entry.get("slot"), entry.get("generation")
        if (slot not in self.slots or not isinstance(generation, str) or not GENERATION.fullmatch(generation)
                or (entry.get("uid"), entry.get("image")) != self.slots[slot]):
            return None
        return entry

    def staged(self, state, stage):
        row = self.current(state.get("staged"))
        if (row and row["generation"] == stage.generation and row["uid"] == stage.policy
                and f"{self.namespace}/agy-{row['slot']}" == stage.namespace):
            return row
        return None

    def active(self, home):
        with self.locked(home) as state:
            current = self.current(state.get("active"))
            return current["generation"] if current else None

    def promote(self, home, stage):
        with self.locked(home) as state:
            row = self.staged(state, stage)
            if not row:
                return False
            state["active"] = row
            del state["staged"]
            self.save(state)
            return True

    def discard(self, home, stage):
        with self.locked(home) as state:
            row = self.staged(state, stage)
            if row:
                del state["staged"]
                self.save(state)
                self.control(row["slot"], "clear", stage.generation)

    def withdraw(self, home, generation, _reason=None):
        with self.locked(home) as state:
            row = self.current(state.get("active"))
            if row and row["generation"] == generation:
                del state["active"]
                self.save(state)
                self.control(row["slot"], "clear", generation)

    def spawn_staged(self, home, stage, operation, request):
        with self.locked(home) as state:
            row = self.staged(state, stage)
            if not row or operation not in {"login", "probe"}:
                raise AgyError("unavailable")
            return self.launch(row, operation, request)

    def spawn(self, home, generation, operation, request):
        with self.locked(home) as state:
            row = self.current(state.get("active"))
            if not row or operation != "inference" or row["generation"] != generation:
                raise AgyError("auth_required")
            return self.launch(row, operation, request)

    def lifecycle(self, slot):
        return SandboxLifecycle(self.home, self.kubectl, self.namespace, IDLE_SECONDS, workload=f"agy-{slot}")

    def command(self, slot, action, generation):
        return [self.kubectl, "--namespace", self.namespace, "exec", "-i", f"agy-{slot}-0", "--container", "agy",
                "--", "/opt/hermes/.venv/bin/python", "-I", "-c", POD_ENTRY, action, generation]

    def control(self, slot, action, generation):
        try:
            with self.lifecycle(slot).active():
                capture(self.command(slot, action, generation))
        except EnvironmentConnectionError:
            raise AgyError("unavailable") from None

    def launch(self, row, operation, request):
        if operation == "login" and request == LOGIN_COMMAND:
            payload = request_frame("login")
        elif operation in {"probe", "inference"} and isinstance(request, str):
            command = "acp" if operation == "inference" and self.acp else "print"
            payload = request_frame(command, request)
        else:
            raise AgyError("unavailable")
        slot = row["slot"]
        try:
            with self.lifecycle(slot).active() as (_, fd):
                process = subprocess.Popen(self.command(slot, "launch", row["generation"]),
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True, pass_fds=(fd,))
        except (OSError, EnvironmentConnectionError):
            raise AgyError("unavailable") from None
        process.agy_request_frame = payload
        return process
