"""Owned HTTP relay EOF is bounded while ordinary desktop TCP half-close remains valid."""

import socket
import subprocess
import sys
import threading

import pytest

from tools.environments import streams
from tools.thread_context import propagate_context_to_thread


@pytest.mark.parametrize("grace", [None, 0.5])
def test_final_response_after_half_close_is_delivered(monkeypatch, grace):
    exercise(monkeypatch, grace, reply=True)


def test_http_owned_silent_peer_cannot_leak_its_remote_relay(monkeypatch):
    exercise(monkeypatch, 0.15, reply=False)


def exercise(monkeypatch, grace, *, reply):
    release = threading.Event()
    processes = []
    def launch(_environment, argv, **_kwargs):
        process = subprocess.Popen([sys.executable, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process
    monkeypatch.setattr(streams, "open_stream", launch)
    with socket.socket() as peer:
        peer.bind(("127.0.0.1", 0)); peer.listen(1)
        def serve():
            connection, _ = peer.accept()
            with connection:
                while connection.recv(1024):
                    pass
                if reply:
                    connection.sendall(b"final-response")
                else:
                    release.wait(3)
        worker = threading.Thread(target=propagate_context_to_thread(serve), daemon=True)
        worker.start()
        forward = streams.PortForward(object(), "127.0.0.1", peer.getsockname()[1], user=None, close_grace=grace)
        try:
            with socket.create_connection(("127.0.0.1", forward.local_port), timeout=3) as client:
                client.sendall(b"request")
                client.shutdown(socket.SHUT_WR)
                assert client.recv(1024) == (b"final-response" if reply else b"")
            assert len(processes) == 1
            processes[0].wait(timeout=2)
        finally:
            release.set(); forward.close(); worker.join(timeout=3)
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=2)
