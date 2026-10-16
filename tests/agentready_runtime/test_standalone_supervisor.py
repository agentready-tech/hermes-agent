"""Real child restarts and single foreground ownership on POSIX hosts."""

import os
import json
import signal
import subprocess
import sys
import threading

import pytest

from agentready_runtime.standalone.supervisor import Supervisor, alive, fingerprint, owner_lock
from tests.waiting import wait_until


@pytest.mark.platforms("posix")
def test_only_failed_child_restarts_and_shutdown_reaps_all(tmp_path):
    worker = [sys.executable, "-c", "import threading; threading.Event().wait()"]
    supervisor = Supervisor(tmp_path, {"backend": worker, "gateway": worker}, port=9119, restart_delay=0)
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    try:
        wait_until(lambda: len(supervisor.children) == 2, timeout=10, what="two child processes")
        gateway = supervisor.children["gateway"].pid
        backend = supervisor.children["backend"].pid
        os.kill(backend, signal.SIGKILL)
        wait_until(lambda: supervisor.children.get("backend") is not None and supervisor.children["backend"].pid != backend,
                   timeout=10, what="replacement backend")
        assert supervisor.children["gateway"].pid == gateway
        with pytest.raises(RuntimeError):
            with owner_lock(tmp_path):
                pytest.fail("second owner acquired the same home")
    finally:
        supervisor.stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert all(child.poll() is not None for child in supervisor.children.values())


def test_status_rejects_reused_pid():
    identity = fingerprint(os.getpid())
    assert alive(identity)
    identity["created_at"] -= 10
    assert not alive(identity)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # Real orphan PIDs leave pytest's ancestry; only this temp owner's fingerprints are used.
def test_new_owner_reaps_verified_orphans_after_owner_crash(tmp_path):
    script = """
import os,sys
from pathlib import Path
from agentready_runtime.standalone.supervisor import Supervisor, owner_lock
home=Path(sys.argv[1])
with owner_lock(home):
    owner=Supervisor(home, {'backend':[sys.executable,'-c','import threading;threading.Event().wait()']}, port=9119)
    owner.tick()
    os._exit(23)
"""
    parent = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    assert parent.returncode == 23
    previous = json.loads((tmp_path / "autonomous" / "status.json").read_text())
    child = previous["children"]["backend"]
    replacement = Supervisor(tmp_path, {"backend": [sys.executable, "-c", "import threading;threading.Event().wait()"]}, port=9119)
    thread = threading.Thread(target=replacement.run)
    thread.start()
    try:
        wait_until(lambda: bool(replacement.children), timeout=10, what="reclaimed dead-owner children")
        assert not alive(child)
        assert replacement.children["backend"].pid != child["pid"]
    finally:
        replacement.stop.set()
        thread.join(timeout=10)
