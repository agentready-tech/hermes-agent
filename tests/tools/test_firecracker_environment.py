"""Exercise the opt-in backend through its real Unix-socket client."""

import json
import socketserver
import struct
import subprocess
import threading
import tempfile
from pathlib import Path

from tools.bot_desktop import placement
from tools.environments import firecracker, streams
from tools.environments.firecracker_client import HEADER
from tools.terminal_tool_backends import _create_environment


def test_opt_in_backend_shares_transport_with_desktop(monkeypatch, tmp_path):
    calls = []
    state = {"generation": "a" * 32}

    class Worker(socketserver.StreamRequestHandler):
        def handle(self):
            size = struct.unpack("!I", self.rfile.read(4))[0]
            request = json.loads(self.rfile.read(size))
            calls.append(request)
            operation = request["operation"]
            reply = {"ok": True, "generation": state["generation"]}
            if operation == "exec":
                assert request["generation"] == state["generation"]
            payload = json.dumps(reply).encode()
            self.wfile.write(struct.pack("!I", len(payload)) + payload)
            if operation != "exec":
                return
            result = subprocess.run(request["argv"], input=self.rfile.read(), capture_output=True)
            for channel, data in ((1, result.stdout), (2, result.stderr), (3, bytes([result.returncode]))):
                self.wfile.write(HEADER.pack(channel, len(data)) + data)

    with (tempfile.TemporaryDirectory(prefix="fc-", dir="/tmp") as socket_dir,
          socketserver.ThreadingUnixStreamServer(str(Path(socket_dir) / "w"), Worker) as worker):
        path = Path(worker.server_address)
        thread = threading.Thread(target=worker.serve_forever)
        thread.start()
        try:
            monkeypatch.setattr(firecracker, "profile_socket", lambda: path)
            monkeypatch.setattr(firecracker.FirecrackerEnvironment, "get_temp_dir", lambda _: str(tmp_path))
            monkeypatch.setattr(placement, "_terminal_backend", lambda: "firecracker")
            probe = _create_environment("firecracker", "", str(tmp_path), 10, probe_only=True)
            assert streams.supports_streams(probe)
            assert [call["operation"] for call in calls] == ["status"]
            env = _create_environment("firecracker", "", str(tmp_path), 10)
            result = env.execute("cat; exit 7", stdin_data="hello\n")
            assert result["output"].strip() == "hello"
            assert result["returncode"] == 7
            assert placement.resolve().where == placement.TERMINAL
            stream = streams.open_stream(env, ["cat"], user="pn")
            assert stream.communicate(b"RFB\x00\n", timeout=5)[0] == b"RFB\x00\n"
            assert calls[-1]["user"] == "pn"

            old = env.firecracker_identity
            state["generation"] = "b" * 32
            assert not firecracker.marker_alive(old)
            assert env.execute("true")["environment_recreated"]
            assert firecracker.marker_alive(env.firecracker_identity)
        finally:
            worker.shutdown()
            thread.join()
