"""Antigravity worker operations on one already isolated credential slot."""

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from hermes_runtime.standalone import agy_acp_worker
from hermes_runtime.standalone.agy_keyring import private_keyring
from hermes_runtime.standalone.agy_runtime import LOGIN_COMMAND, child_environment
from providers.agy_policy import AgyError, PROMPT_LIMIT
from providers.agy_process import close_child

MAX_FRAME = 2 * PROMPT_LIMIT + 128  # The frame adds JSON escaping to the encoded prompt.
MAX_CODE = 4096
GENERATION = re.compile(r"[a-f0-9]{32}")


def request_frame(operation, prompt=None):
    data = json.dumps({"operation": operation, "prompt": prompt}, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise AgyError("input_too_large")
    return f"{len(data):08d}".encode("ascii") + data


def read_exact(fd, size):
    data = bytearray()
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise AgyError("cancelled")
        data.extend(chunk)
    return data


def read_command(fd):
    size = read_exact(fd, 8)
    if not size.isdigit() or not 0 < int(size) <= MAX_FRAME:
        raise AgyError("protocol")
    data = json.loads(read_exact(fd, int(size)))
    if not isinstance(data, dict) or set(data) != {"operation", "prompt"}:
        raise AgyError("protocol")
    operation, prompt = data["operation"], data["prompt"]
    if operation == "login" and prompt is None:
        return LOGIN_COMMAND, None
    if operation != "acp" or not isinstance(prompt, str) or not prompt or "\x00" in prompt:
        raise AgyError("protocol")
    if len(prompt.encode("utf-8")) > PROMPT_LIMIT:
        raise AgyError("input_too_large")
    return agy_acp_worker.ACP_COMMAND, prompt


def forward_code(child, pending):
    """Deliver exactly one printable code to the child, terminated by one LF."""
    if not pending.endswith(b"\n"):
        if len(pending) > MAX_CODE:
            raise AgyError("protocol")
        return
    code = pending[:-1]
    if not 0 < len(code) <= MAX_CODE or any(c < 33 or c > 126 for c in code):
        raise AgyError("protocol")
    written = os.write(child.stdin.fileno(), pending)
    if written != len(pending):
        raise AgyError("transport")
    child.stdin.close()
    pending.clear()


def supervise_login(child, incoming, *, timeout=900):
    deadline = time.monotonic() + timeout
    pending = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(incoming, selectors.EVENT_READ)
            while (status := child.poll()) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgyError("transport")
                if not selector.select(min(0.05, remaining)):
                    continue
                data = os.read(incoming, MAX_CODE + 1)
                if not data:
                    raise AgyError("cancelled")
                if child.stdin.closed:
                    raise AgyError("protocol")
                pending.extend(data)
                forward_code(child, pending)
            return status if status >= 0 else 128 - status
    finally:
        close_child(child)


def run(store, scratch, incoming, *, launcher):
    argv, payload = read_command(incoming)
    home = scratch / "home"
    workdir = scratch / "work"
    home.mkdir(mode=0o700)
    workdir.mkdir(mode=0o700)
    acp = payload is not None
    if acp:
        agy_acp_worker.acp_settings(home)
    environment = child_environment(home)
    environment["TMPDIR"] = str(workdir)
    os.set_blocking(incoming, False)
    with private_keyring(store, scratch) as address:
        environment["DBUS_SESSION_BUS_ADDRESS"] = address
        command, environment = launcher(home, workdir, address, argv, environment)
        child = subprocess.Popen(command, cwd=workdir, env=environment, start_new_session=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE if acp else None,
                                 stderr=subprocess.PIPE if acp else None)
        if acp:
            return agy_acp_worker.run(child, incoming, payload, Path("/work"))
        os.set_blocking(child.stdin.fileno(), False)
        return supervise_login(child, incoming)


def operate(action, generation, *, auth, launcher=None):
    import fcntl

    if not GENERATION.fullmatch(generation):
        raise AgyError("unavailable")
    marker = auth / "generation"
    home = auth / "home"
    with (auth / "worker.lock").open("a") as lock:
        # A slot has one keyring worker at a time.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "prepare":
            marker.unlink(missing_ok=True)
            if home.exists():
                shutil.rmtree(home)
            home.mkdir(mode=0o700)
            marker.write_text(generation, encoding="ascii")
            return 0
        if not marker.is_file() or marker.read_text(encoding="ascii") != generation:
            raise AgyError("unavailable")
        if action == "clear":
            marker.unlink()
            shutil.rmtree(home)
            return 0
        if action != "launch" or launcher is None:
            raise AgyError("unavailable")
        with TemporaryDirectory(prefix="agy-", dir="/tmp") as scratch:
            return run(home, Path(scratch), sys.stdin.fileno(), launcher=launcher)
