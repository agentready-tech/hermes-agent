"""Framed stdin execution keeps sandbox commands and forwarded secrets out of API URLs."""

import json


RUN = r'''
import json, os, signal, subprocess, sys
from pathlib import Path

def read_exact(size):
    data = bytearray()
    while len(data) < size:
        chunk = os.read(0, size - len(data))
        if not chunk:
            raise EOFError("incomplete command frame")
        data.extend(chunk)
    return bytes(data)

def birth(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None

length = int(read_exact(8))
if not 0 < length <= 16 * 1024 * 1024:
    raise SystemExit("invalid command frame size")
request = json.loads(read_exact(length))
record = Path(request["record"])
cancelled = record.with_suffix(".cancel")
if cancelled.exists():
    cancelled.unlink()
    raise SystemExit(130)
environment = dict(os.environ)
for name in request["unset"]:
    environment.pop(name, None)
environment.update(request["env"])
argv = ["bash", *(["-l"] if request["login"] else []), "-c", request["command"]]
child = subprocess.Popen(argv, env=environment, stdin=0, start_new_session=True)
try:
    fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"pid": child.pid, "birth": birth(child.pid)}, stream)
    if cancelled.exists():
        os.killpg(child.pid, signal.SIGKILL)
    code = child.wait()
finally:
    # A failed record write must not leave an untracked remote command running.
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    record.unlink(missing_ok=True)
    cancelled.unlink(missing_ok=True)
raise SystemExit(code if code >= 0 else 128 - code)
'''

CANCEL = r'''
import json, os, signal, sys
from pathlib import Path
record = Path(sys.argv[1])
record.with_suffix(".cancel").touch(mode=0o600, exist_ok=True)
try:
    value = json.loads(record.read_text())
    pid = value["pid"]
    current = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
    if value["birth"] is not None and value["birth"] == current and os.getpgid(pid) == pid:
        os.killpg(pid, signal.SIGKILL)
except (OSError, ValueError, KeyError, IndexError):
    pass
'''


def frame(command: str, record: str, values: dict, unset: set, login: bool, stdin: str | None) -> str:
    payload = json.dumps({"command": command, "record": record, "env": values,
                          "unset": sorted(unset), "login": login}, ensure_ascii=True)
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("Kubernetes command frame exceeds 16 MiB")
    return f"{len(payload):08d}" + payload + (stdin or "")
