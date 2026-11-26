"""Give the CLI one credential generation and a private working directory."""

import ctypes
import os
from pathlib import Path
import sys

from providers.agy_policy import AgyError, BINARY, SETTINGS

_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_PATH_BENEATH = 1
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1, 2, 4, 8
_READ = _READ_FILE | _READ_DIR


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def confine(home, workdir):
    if sys.platform != "linux" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise AgyError("unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(_CREATE_RULESET, 0, 0, 1)
    if abi < 1:
        raise AgyError("unavailable")
    # The Pod mounts one credential store and a read-only root, even on ABI 1.
    mask = (1 << (13 + (abi >= 2) + (abi >= 3) + (abi >= 5))) - 1
    attributes = ctypes.c_uint64(mask)
    ruleset = libc.syscall(_CREATE_RULESET, ctypes.byref(attributes), ctypes.sizeof(attributes), 0)
    if ruleset < 0:
        raise AgyError("unavailable")
    try:
        for path, modes in ((home, mask & ~_EXECUTE), (workdir, mask & ~_EXECUTE),
                            (BINARY, _READ_FILE | _EXECUTE), (SETTINGS, _READ_FILE),
                            ("/dev/null", _READ_FILE | _WRITE_FILE), ("/dev/urandom", _READ_FILE)):
            allow(libc, ruleset, path, modes)
        for path in ("/lib", "/lib64", "/usr/lib", "/etc/ssl", "/etc/pki"):
            if Path(path).is_dir():
                allow(libc, ruleset, path, _READ)
        for path in ("/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf", "/etc/passwd", "/etc/machine-id",
                     "/etc/os-release", "/etc/ld.so.cache", "/lib64/ld-linux-x86-64.so.2",
                     "/lib/ld-linux-aarch64.so.1"):
            if Path(path).is_file():
                modes = _READ_FILE | (_EXECUTE if "ld-linux" in path else 0)
                allow(libc, ruleset, path, modes)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(_RESTRICT_SELF, ruleset, 0):
            raise AgyError("unavailable")
    finally:
        os.close(ruleset)


def allow(libc, ruleset, path, modes):
    descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = PathBeneath(modes, descriptor)
        if libc.syscall(_ADD_RULE, ruleset, _PATH_BENEATH, ctypes.byref(rule), 0):
            raise AgyError("unavailable")
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    home, workdir, *argv = sys.argv[1:]
    try:
        confine(home, workdir)
        os.execv(BINARY, argv)
    except (OSError, AgyError):
        raise SystemExit(125) from None
