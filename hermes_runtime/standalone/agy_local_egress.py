"""Carry TLS to the configured endpoints from a loopback-only user namespace."""

from contextlib import contextmanager, suppress
import fcntl
import os
import select
import selectors
import socket
import struct
from threading import BoundedSemaphore, Event, Thread

PORT = 443
IDLE_TIMEOUT = 120


def enable_loopback():
    with socket.socket() as sock:
        request = struct.pack("16sH22x", b"lo", 0)
        flags = struct.unpack("16sH22x", fcntl.ioctl(sock.fileno(), 0x8913, request))[1]
        fcntl.ioctl(sock.fileno(), 0x8914, struct.pack("16sH22x", b"lo", flags | 1))


def loopback_endpoint(index):
    return f"127.0.0.{index + 2}"


def forward(left, right):
    left.settimeout(IDLE_TIMEOUT)
    right.settimeout(IDLE_TIMEOUT)
    peers = {left: right, right: left}
    while peers:
        readable, _, _ = select.select(list(peers), [], [], IDLE_TIMEOUT)
        if not readable:
            return
        for source in readable:
            dest = peers[source]
            if data := source.recv(65536):
                dest.sendall(data)
            else:
                del peers[source]
                with suppress(OSError):
                    dest.shutdown(socket.SHUT_WR)


@contextmanager
def accept_connections(listeners, connect):
    stopped = Event()
    capacity = BoundedSemaphore(24)

    def handle(client, target):
        try:
            with client:
                client.settimeout(10)
                with connect(client, target) as remote:
                    forward(client, remote)
        except OSError:
            pass
        finally:
            capacity.release()

    with selectors.DefaultSelector() as ready:
        for listener, target in listeners:
            ready.register(listener, selectors.EVENT_READ, target)

        def serve():
            while not stopped.is_set():
                for key, _ in ready.select(0.1):
                    client, _ = key.fileobj.accept()
                    if capacity.acquire(blocking=False):
                        Thread(target=handle, args=(client, key.data), daemon=True).start()
                    else:
                        client.close()

        thread = Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join()


@contextmanager
def outbound_relay(path, addresses, uid):
    """The trusted parent is the only side with access to external networking."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        os.chown(path, uid, uid)
        os.chmod(path, 0o600)
        listener.listen()

        def connect(client, _target):
            index = client.recv(1)
            if not index or index[0] >= len(addresses):
                raise OSError("Unknown Antigravity destination")
            return socket.create_connection((addresses[index[0]], PORT), timeout=10)

        with accept_connections([(listener, None)], connect):
            yield


@contextmanager
def namespace_relay(path, count):
    enable_loopback()
    listeners = []
    try:
        for index in range(count):
            listener = socket.socket()
            listeners.append((listener, index))
            listener.bind((loopback_endpoint(index), PORT))
            listener.listen()

        def connect(_client, index):
            remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                remote.settimeout(10)
                remote.connect(str(path))
                remote.sendall(bytes([index]))
                return remote
            except OSError:
                remote.close()
                raise

        with accept_connections(listeners, connect):
            yield
    finally:
        for listener, _ in listeners:
            listener.close()
