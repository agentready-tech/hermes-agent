"""Deterministic login and ACP broker fixtures using real POSIX pipes."""

import json
from pathlib import Path
import signal
import subprocess
import sys


def spawn(cwd, mode, *args, login=False):
    return subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), mode, *args],
                            cwd=cwd, start_new_session=True, env={"HOME": str(cwd), "LANG": "C.UTF-8"},
                            stdin=subprocess.PIPE if login else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def spawn_acp(cwd, steps, payload, *, require_store=False):
    from hermes_runtime.standalone.agy_slot import request_frame

    child = spawn(cwd, "acp", json.dumps(steps), "store" if require_store else "", login=True)
    child.agy_request_frame = request_frame("acp", payload)
    return child


def broker(steps, require_store):
    marker = Path("fixture-store-ready")
    if require_store and (not marker.is_file() or marker.read_text() != "synthetic only"):
        return 6
    incoming = sys.stdin.buffer
    frame = json.loads(incoming.read(int(incoming.read(8))))
    assert frame["operation"] == "acp"
    pending = None
    for mode, response in steps:
        if pending is not None:
            result = json.loads(incoming.readline())
            assert result["type"] == "tool_result" and result["id"] == pending
            if mode == "canonical-followup":
                assert result["content"] == f'{{"text": "{response}"}}'
                response = '{"type":"final","answer":"Проверено ☃"}'
        if mode in {"error", "error-diagnostics", "containment"}:
            row = {"type": "error", "reason": "containment" if mode == "containment" else "transport",
                   "stage": "session/prompt"}
            if mode == "error-diagnostics":
                row.update({
                    "detail": "Provider deadline exceeded. https://fixture.invalid/?code=fixture-oauth-code",
                    "stderr": "Upstream connection reset. qa.fixture@example.test ya29.fixture-access-token",
                })
            print(json.dumps(row), flush=True)
            return 42
        if mode == "nonzero":
            return 42
        if mode == "partial":
            sys.stdout.write(response[:-1])
            return 0
        print(response, flush=True)
        try:
            row = json.loads(response)
        except ValueError:
            return 0
        if row.get("type") != "tool_call":
            return 0
        pending = row.get("id")
        if pending and pending.startswith("call_"):
            pending = "agy-" + pending.removeprefix("call_")
    return 0


def child(mode, response="", payload=""):
    marker = Path("fixture-store-ready")
    if mode == "acp":
        return broker(json.loads(response), payload == "store")
    if mode in {"login", "store-login", "invalid-grant"}:
        print(response, flush=True)
        if sys.stdin.buffer.read() != b"fixture-only\n":
            return 5
        if mode == "invalid-grant":
            print("invalid_grant fixture diagnostic", file=sys.stderr)
            return 3
        if mode == "store-login":
            marker.write_text("synthetic only")
        return 0
    if mode == "tree":
        subprocess.Popen([sys.executable, "-I", "-c", "import signal; signal.pause()"])
        signal.pause()
        return 0
    raise AssertionError("unknown fixture mode")


if __name__ == "__main__":
    sys.exit(child(*sys.argv[1:]))
