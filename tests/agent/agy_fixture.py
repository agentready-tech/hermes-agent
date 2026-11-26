"""Only deterministic frames, an echo result, and POSIX pipe fixtures for AGE-11."""

import json
import os
from pathlib import Path
import subprocess
import sys
import signal


def frame(kind, value, **extra):
    return json.dumps({"event": kind, kind: value, **extra}, ensure_ascii=False).encode() + b"\n"


def spawn(cwd, mode, *args, login=False):
    return subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), mode, *args],
                            cwd=cwd, start_new_session=True, env={"HOME": str(cwd), "LANG": "C.UTF-8"},
                            stdin=subprocess.PIPE if login else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def child(mode, response='{"type":"final","answer":"Ответ ☃"}', prompt=""):
    marker = Path("fixture-store-ready")
    if mode.startswith("store-probe:"):
        if not marker.is_file() or marker.read_text() != "synthetic only":
            return 6
        mode = mode.removeprefix("store-probe:")
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
    if mode == "stderr":
        sys.stderr.buffer.write(b"x" * 70000)
    if mode == "soft-deny":
        print("fixture permission denied", file=sys.stderr)
    if mode == "large":
        sys.stdout.buffer.write(b"x" * (257 * 1024))
        return 0
    if mode == "canonical-followup":
        rows = json.loads(prompt)["messages"]
        assert rows[-1]["role"] == "tool" and rows[-1]["tool_call_id"] == response
        assert rows[-1]["content"] == f'{{"text": "{response}"}}'
        response = '{"type":"final","answer":"Проверено ☃"}'
    init = frame("init", {"cwd": "/fixture", "tools": [], "permission_mode": "request-review"}, conversation_id="fixture")
    step = frame("step_update", {"step_type": "agent_response", "step_index": 1, "state": "DONE", "text_delta": "ignored ☃"})
    result = frame("result", {"status": "ERROR" if mode == "error" else "SUCCESS", "conversation_id": "wrong" if mode == "identity" else "fixture", "response": response, "num_turns": 12})
    if mode == "native":
        step = frame("step_update", {"step_type": "tool", "tool_info": {"kind": "inert-marker"}})
    if mode == "repeat":
        result *= 2
    if mode == "partial":
        result = result[:-1]
    if mode == "out-of-order":
        init, step = step, init
    if mode == "outer-dupe":
        init = init.replace(b'"tools": []', b'"tools": [], "tools": []')
    if mode == "no-final":
        result = b""
    blob = init + step + result
    offset = blob.find("☃".encode()) + 1
    os.write(1, blob[:offset])
    os.write(1, blob[offset:])
    return 42 if mode == "nonzero" else 0


if __name__ == "__main__":
    sys.exit(child(*sys.argv[1:]))
