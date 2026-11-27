"""Profile-bound access to Antigravity stores in the shared Hermes runtime."""

from contextlib import contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
from threading import Lock
import time

from hermes_constants import get_default_hermes_root, named_profile_is_live
from hermes_runtime.standalone.agy_local_worker import LOCAL_CONFIG, LOCAL_STORE, SLOT_UID, command
from hermes_runtime.standalone.agy_runtime import LOGIN_COMMAND, StagedStore
from hermes_runtime.standalone.agy_slot import GENERATION, request_frame
from providers.agy_acp_wire import Peer
from providers.agy_policy import AgyError, valid_model_id
from providers.agy_process import close_child
from utils import atomic_json_write

SLOTS = ("a", "b")
SA_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


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
    return profile


def authority_for(home):
    try:
        if SA_NAMESPACE.read_text().strip() != "agentsee-provisioning-test" or not LOCAL_CONFIG.is_file():
            raise AgyError("unavailable")
        authority = local_authority(owned_profile(home))
        authority.profile_store()
        return authority
    except (OSError, TypeError, ValueError):
        raise AgyError("unavailable") from None


@lru_cache(maxsize=128)
def local_authority(home):
    return LocalAuthority(home)


class LocalAuthority:
    def __init__(self, home):
        self.home = home
        self.root = home.parent.parent / "agy-bindings" / home.name
        self.binding = self.root / "binding.json"
        self._catalog_lock = Lock()
        self._catalog = None

    def profile_store(self):
        parent = LOCAL_STORE / self.home.name
        if parent.resolve(strict=True) != parent:
            raise AgyError("unavailable")
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AgyError("unavailable")
        return parent

    @contextmanager
    def store(self, slot):
        if slot not in SLOTS:
            raise AgyError("unavailable")
        descriptor = os.open(self.profile_store() / slot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid) != (SLOT_UID, SLOT_UID) or info.st_mode & 0o077:
                raise AgyError("unavailable")
            yield descriptor
        finally:
            os.close(descriptor)

    def stage(self, home):
        with self.locked(home) as state:
            current = self.current(state.get("active"))
            slot = "b" if current and current["slot"] == "a" else "a"
            generation = secrets.token_hex(16)
            self.control(slot, "prepare", generation)
            state["staged"] = {"slot": slot, "generation": generation}
            self.save(state)
            return StagedStore(generation, self.home, slot)

    def current(self, entry):
        if not isinstance(entry, dict):
            return None
        slot, generation = entry.get("slot"), entry.get("generation")
        if slot not in SLOTS or not isinstance(generation, str) or not GENERATION.fullmatch(generation):
            return None
        # Legacy bindings carry UID/image fields. The existing private slot and
        # its generation are the durable identity, including across rollouts.
        try:
            with self.store(slot) as descriptor:
                try:
                    fd = os.open("generation", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                except FileNotFoundError:
                    return None
                with os.fdopen(fd, "rb") as marker:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != SLOT_UID or info.st_size != len(generation):
                        raise AgyError("unavailable")
                    return entry if marker.read(len(generation) + 1) == generation.encode("ascii") else None
        except OSError:
            raise AgyError("unavailable") from None

    def staged(self, state, stage):
        if stage.profile != self.home:
            return None
        row = self.current(state.get("staged"))
        if row and (row["slot"], row["generation"]) == (stage.slot, stage.generation):
            return row
        return None

    def active(self, home):
        with self.locked(home) as state:
            current = self.current(state.get("active"))
            return current["generation"] if current else None

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
            if not row or row["generation"] != generation:
                raise AgyError("auth_required")
            if operation not in {"inference", "models"}:
                raise AgyError("unavailable")
            return self.launch(row, operation, request)

    def models(self, home, generation):
        """Reuse discovery briefly within one active profile credential generation."""
        with self._catalog_lock:
            if generation is None or self.active(home) != generation:
                raise AgyError("auth_required")
            cached = self._catalog
            if cached is not None and cached[0] == generation and cached[1] > time.monotonic():
                return cached[2]
            child = self.spawn(home, generation, "models", None)
            peer = Peer(child)
            try:
                peer.send(child.agy_request_frame)
                deadline = time.monotonic() + 50
                row = peer.receive(lambda: deadline, lambda: False)
                models = row.get("models")
                if row.get("type") != "models" or not isinstance(models, list) or not 0 < len(models) <= 200:
                    raise AgyError("protocol")
                catalog = []
                for model in models:
                    if (not isinstance(model, dict) or not valid_model_id(model.get("id"))
                            or not isinstance(model.get("label"), str)):
                        raise AgyError("protocol")
                    catalog.append((model["id"], model["label"]))
            finally:
                peer.close()
            if authority_for(home) is not self or self.active(home) != generation:
                raise AgyError("auth_required")
            result = tuple(catalog)
            self._catalog = generation, time.monotonic() + 60, result
            return result

    def payload(self, operation, request):
        if operation == "login" and request == LOGIN_COMMAND:
            return request_frame("login")
        if operation in {"probe", "models"} and request is None:
            return request_frame("acp", json.dumps({"type": operation}))
        if operation == "inference" and isinstance(request, str):
            return request_frame("acp", request)
        raise AgyError("unavailable")

    def control(self, slot, action, generation):
        try:
            with self.store(slot) as fd:
                child = subprocess.Popen(command("start", action, generation, fd, LOCAL_CONFIG), pass_fds=(fd,),
                                         start_new_session=True, env={"LANG": "C.UTF-8"},
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                if child.wait(timeout=12) != 0:
                    raise AgyError("unavailable")
            finally:
                close_child(child)
        except (OSError, subprocess.SubprocessError):
            raise AgyError("unavailable") from None

    def launch(self, row, operation, request):
        payload = self.payload(operation, request)
        try:
            with self.store(row["slot"]) as fd:
                process = subprocess.Popen(command("start", "launch", row["generation"], fd, LOCAL_CONFIG),
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env={"LANG": "C.UTF-8"}, start_new_session=True, pass_fds=(fd,))
        except OSError:
            raise AgyError("unavailable") from None
        process.agy_request_frame = payload
        return process
