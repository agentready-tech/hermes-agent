"""Execute the fixed Antigravity commands in the profile's dedicated Pod.

The caller keeps the exec input stream open until the request completes. Losing
the stream cancels the remote process group, including an in-progress login.
"""

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

from hermes_runtime.standalone.agy_keyring import private_keyring
from hermes_runtime.standalone.agy_runtime import LOGIN_COMMAND, PRINT_COMMAND, child_environment
from providers.agy_policy import AgyError, PRINT_DONE, PROMPT_LIMIT
from providers.agy_process import close_child

MAX_FRAME = 2 * PROMPT_LIMIT + 128  # The frame adds JSON escaping to the encoded prompt.
MAX_CODE = 4096
GENERATION = re.compile(r"[a-f0-9]{32}")
AUTH = Path("/auth")


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
    if operation != "print" or not isinstance(prompt, str) or not prompt or "\x00" in prompt:
        raise AgyError("protocol")
    if len(prompt.encode("utf-8")) > PROMPT_LIMIT:
        raise AgyError("input_too_large")
    message = {"event": "user", "message": {"content": prompt}}
    return PRINT_COMMAND, json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


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


def supervise(child, incoming, *, timeout, print_input=None):
    deadline = time.monotonic() + timeout
    pending = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(incoming, selectors.EVENT_READ)
            if print_input is not None:
                pending_prompt = memoryview(print_input)
                selector.register(child.stdin, selectors.EVENT_WRITE)
            while (status := child.poll()) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgyError("transport")
                for key, _ in selector.select(min(0.05, remaining)):
                    if key.events == selectors.EVENT_WRITE:
                        if child.stdin.closed:
                            continue
                        pending_prompt = pending_prompt[os.write(key.fd, pending_prompt):]
                        if not pending_prompt:
                            selector.unregister(child.stdin)
                        continue
                    data = os.read(incoming, MAX_CODE + 1)
                    if not data:
                        raise AgyError("cancelled")
                    if child.stdin is None or child.stdin.closed:
                        raise AgyError("protocol")
                    if print_input is None:
                        pending.extend(data)
                        forward_code(child, pending)
                    elif data == PRINT_DONE:
                        if child.stdin.fileno() in selector.get_map():
                            selector.unregister(child.stdin)
                        child.stdin.close()
                    else:
                        raise AgyError("protocol")
            return status if status >= 0 else 128 - status
    finally:
        close_child(child)


def run(store, scratch, incoming):
    argv, print_input = read_command(incoming)
    home = scratch / "home"
    workdir = scratch / "work"
    home.mkdir(mode=0o700)
    workdir.mkdir(mode=0o700)
    environment = child_environment(home)
    environment["TMPDIR"] = str(workdir)
    source = Path(__file__).resolve().parents[2]
    invocation = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                  "import runpy; runpy.run_module('hermes_runtime.standalone.agy_confine', run_name='__main__')")
    os.set_blocking(incoming, False)
    with private_keyring(store, scratch) as address:
        environment["DBUS_SESSION_BUS_ADDRESS"] = address
        child = subprocess.Popen([sys.executable, "-I", "-c", invocation, str(source),
                                  str(home), str(workdir), *argv],
                                 cwd=workdir, env=environment, start_new_session=True,
                                 stdin=subprocess.PIPE)
        os.set_blocking(child.stdin.fileno(), False)
        return supervise(child, incoming, timeout=900 if print_input is None else 120, print_input=print_input)


def operate(action, generation):
    import fcntl

    if not GENERATION.fullmatch(generation):
        raise AgyError("unavailable")
    marker = AUTH / "generation"
    home = AUTH / "home"
    with (AUTH / "worker.lock").open("a") as lock:
        # Isolate processes within the same slot as well as the two Pod mounts.
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
        if action != "launch":
            raise AgyError("unavailable")
        with TemporaryDirectory(prefix="agy-", dir="/tmp") as scratch:
            return run(home, Path(scratch), sys.stdin.fileno())


def main():
    if len(sys.argv) != 3:
        raise SystemExit(125)
    try:
        return operate(*sys.argv[1:])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
