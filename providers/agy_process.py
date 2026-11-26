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
    except PermissionError:
        # Darwin can report EPERM for an exiting group. Reap and recheck below.
        pass
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            child.wait(timeout=0.5)  # Exit may race the poll above.
            return
        except PermissionError:
            # A signal-0 EPERM still means the group exists.
            pass
        time.sleep(0.02)
    with suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)
    child.wait(timeout=0.5)


def communicate(child, consume, *, timeout, cancelled, input_data=None, diagnostics: bytearray | None = None):
    """Drain both pipes. A caller-owned buffer retains stderr even when the drain raises."""
    deadline = time.monotonic() + timeout
    if diagnostics is None:
        diagnostics = bytearray()
    prefix = getattr(child, "agy_request_frame", b"")
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, consume)
            selector.register(child.stderr, selectors.EVENT_READ, diagnostics.extend)
            if prefix or input_data is not None:
                selector.register(child.stdin, selectors.EVENT_WRITE, None)
                os.set_blocking(child.stdin.fileno(), False)
            return _drain(child, selector, deadline, cancelled, diagnostics, input_data, prefix)
    except (OSError, subprocess.SubprocessError):
        raise AgyError("transport") from None
    finally:
        close_child(child)


def close_child(child):
    for pipe in (child.stdin, child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()
    stop(child)


def _drain(child, selector, deadline, cancelled, diagnostics, input_data, prefix):
    remaining = prefix
    sending_prefix = bool(prefix)
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
                    if sending_prefix and input_data is not None:
                        sending_prefix = False
                        continue
                    selector.unregister(key.fileobj)
                    if not prefix:
                        key.fileobj.close()
            else:
                data = os.read(key.fd, 16 * 1024)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                key.data(data)
                if len(diagnostics) > STDERR_LIMIT:
                    raise AgyError("transport")
