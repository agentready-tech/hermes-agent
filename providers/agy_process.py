"""Bounded POSIX pipe ownership. Only called with an already-admitted child."""

import os
import selectors
import signal
import subprocess
import time
from contextlib import suppress

from providers.agy_policy import AgyError

STDERR_LIMIT = 64 * 1024


def stop(child):
    """Stop the group, not just the leader: a descendant may hold the pipes."""
    group = child.pid
    if group <= 0:
        raise AgyError("transport")  # Never signal the caller's group (0) or a wildcard PID.
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        child.wait(timeout=2)
        return
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    with suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)
    child.wait(timeout=0.5)


def communicate(child, consume, *, timeout, cancelled, input_data=None):
    deadline = time.monotonic() + timeout
    diagnostics = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, consume)
            selector.register(child.stderr, selectors.EVENT_READ, diagnostics.extend)
            if input_data is not None:
                # A login is the only operation with an input pipe. The callback
                # returns once; the original bytes are not retained after write.
                selector.register(child.stdin, selectors.EVENT_WRITE, None)
                os.set_blocking(child.stdin.fileno(), False)
            return _drain(child, selector, deadline, cancelled, diagnostics, input_data)
    except (OSError, subprocess.SubprocessError):
        raise AgyError("transport") from None
    finally:
        close_child(child)


def close_child(child):
    for pipe in (child.stdin, child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()
    stop(child)


def _drain(child, selector, deadline, cancelled, diagnostics, input_data):
    remaining = b""
    while True:
        if cancelled():
            raise AgyError("cancelled")
        now = time.monotonic()
        if now >= deadline:
            raise AgyError("transport")
        status = child.poll()
        if status is not None and not selector.get_map():
            return status, bytes(diagnostics)
        for key, _ in selector.select(min(0.05, deadline - now)):
            if key.events == selectors.EVENT_WRITE:
                if status is not None or child.poll() is not None:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if not remaining:
                    remaining = input_data() or b""
                    if not remaining:
                        # Avoid spinning on a writable pipe while the human reads.
                        time.sleep(0.02)
                        continue
                remaining = remaining[os.write(key.fd, remaining):]
                if not remaining:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
            else:
                data = os.read(key.fd, 16 * 1024)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                key.data(data)
                if len(diagnostics) > STDERR_LIMIT:
                    raise AgyError("transport")
