"""Bounded JSON lines for the ACP process and its profile-local broker."""

import json
import os
import selectors
import time
from subprocess import SubprocessError

from providers.agy_policy import AgyError, PROMPT_LIMIT
from providers.agy_process import STDERR_LIMIT, close_child

WIRE_LIMIT = 2 * PROMPT_LIMIT + 4096


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def decode(data):
    try:
        obj = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        raise AgyError("protocol") from None
    if not isinstance(obj, dict):
        raise AgyError("protocol")
    return obj


def encode(obj):
    try:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError):
        raise AgyError("protocol") from None
    if len(data) > WIRE_LIMIT:
        raise AgyError("input_too_large")
    return data + b"\n"


class Lines:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        if len(self.buffer) > WIRE_LIMIT + 16384:
            raise AgyError("protocol")

    def pop(self):
        end = self.buffer.find(b"\n")
        if end > WIRE_LIMIT or (end < 0 and len(self.buffer) > WIRE_LIMIT):
            raise AgyError("protocol")
        if end < 0:
            return None
        data = bytes(self.buffer[:end])
        del self.buffer[:end + 1]
        return decode(data)


class Peer:
    """Write while draining stderr and stdout, including a partial pipe write."""

    def __init__(self, child):
        self.child = child
        self.selector = selectors.DefaultSelector()
        self.lines = Lines()
        self.stderr = bytearray()
        self.pending = memoryview(b"")
        self.eof = False
        for name in ("stdout", "stderr"):
            self.selector.register(getattr(child, name), selectors.EVENT_READ, name)
        os.set_blocking(child.stdin.fileno(), False)

    def send(self, data):
        if self.pending:
            raise AgyError("protocol")
        self.pending = memoryview(data)
        self.selector.register(self.child.stdin, selectors.EVENT_WRITE, "stdin")

    def receive(self, deadline, cancelled, tick=lambda: None):
        try:
            while True:
                if cancelled():
                    raise AgyError("cancelled")
                tick()
                row = self.lines.pop()
                if row is not None:
                    return row
                remaining = deadline() - time.monotonic()
                if self.eof or remaining <= 0:
                    raise AgyError("transport")
                for key, _ in self.selector.select(min(0.05, remaining)):
                    if key.data == "stdin":
                        self.pending = self.pending[os.write(key.fd, self.pending):]
                        if not self.pending:
                            self.selector.unregister(key.fileobj)
                    else:
                        self._read(key)
        except (OSError, SubprocessError):
            raise AgyError("transport") from None

    def _read(self, key):
        data = os.read(key.fd, 16384)
        if not data:
            self.selector.unregister(key.fileobj)
            self.eof |= key.data == "stdout"
        elif key.data == "stdout":
            self.lines.feed(data)
        else:
            self.stderr.extend(data[:STDERR_LIMIT - len(self.stderr)])

    def close(self):
        self.selector.close()
        close_child(self.child)
