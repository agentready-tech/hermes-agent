"""Persistent completion must drive the actual main FIFO without a later user message."""
from pathlib import Path
import subprocess
import sys
import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env


@pytest.mark.parametrize('mode', ['normal', 'lost_ack'])
def test_saved_child_wakes_idle_main_without_viewer_and_survives_restart(tmp_path, mode):
    root = Path(__file__).resolve().parents[2]
    env = child_env()
    env['PYTHONPATH'] = str(root)
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/persistent_completion_peer.py'),
                             str(tmp_path), mode], env=env, cwd=root, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=150, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
