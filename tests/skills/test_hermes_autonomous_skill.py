"""Exercise the native CLI schedule path taught by the autonomous skill."""

import json
import os
import subprocess
import sys


def test_skill_schedule_command_creates_native_main_inbox_job():
    result = subprocess.run([sys.executable, "-m", "hermes_runtime.standalone.cli",
        "--home", os.environ["HERMES_HOME"], "schedule", "--when", "every 1h", "--name", "Order review",
        "Review outstanding supplier orders"], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    job = json.loads(result.stdout)
    assert job["origin"] == {"platform": "hermes-autonomous", "chat_id": "main"}
    assert job["prompt"] == "Review outstanding supplier orders"
    assert job["schedule"]["kind"] == "interval"
    assert job["deliver"] == "local"
