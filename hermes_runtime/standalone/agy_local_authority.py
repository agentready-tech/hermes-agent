"""Start confined profile workers in the Helm-selected shared runtime."""

from contextlib import contextmanager
import os
import stat
import subprocess

from hermes_runtime.standalone.agy_local_worker import LOCAL_CONFIG, LOCAL_STORE, SLOT_UID, command
from hermes_runtime.standalone.agy_pod_authority import PodAuthority
from providers.agy_policy import AgyError
from providers.agy_process import close_child


class LocalAuthority(PodAuthority):
    @contextmanager
    def store(self, slot):
        if slot not in self.slots:
            raise AgyError("unavailable")
        parent = LOCAL_STORE / self.home.name
        if parent.resolve(strict=True) != parent:
            raise AgyError("unavailable")
        info = parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AgyError("unavailable")
        descriptor = os.open(parent / slot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (SLOT_UID, SLOT_UID) or info.st_mode & 0o077:
                raise AgyError("unavailable")
            yield descriptor
        finally:
            os.close(descriptor)

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
