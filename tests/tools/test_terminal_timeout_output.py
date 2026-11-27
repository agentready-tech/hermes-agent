"""Verify terminal output capture at high file descriptors and on timeout."""
import shlex
import subprocess
import sys

import pytest

from tools.environments.local import LocalEnvironment


@pytest.fixture
def move_stdout_to_high_descriptor():
    import fcntl
    import os
    import resource

    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit != resource.RLIM_INFINITY and soft_limit <= 1024:
        pytest.skip("The process cannot allocate descriptor 1024")

    def move_stdout(proc):
        with proc.stdout as stdout:
            high_fd = fcntl.fcntl(stdout.fileno(), fcntl.F_DUPFD_CLOEXEC, 1024)
            proc.stdout = os.fdopen(high_fd, "r", encoding="utf-8", errors="replace")

    return move_stdout


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("length", [39, 1024 * 1024])
def test_terminal_captures_stdout_with_high_descriptor(
    tmp_path, monkeypatch, length, move_stdout_to_high_descriptor,
):
    env = LocalEnvironment(cwd=str(tmp_path))
    spawn = env._run_bash

    def spawn_with_high_stdout(*args, **kwargs):
        proc = spawn(*args, **kwargs)
        move_stdout_to_high_descriptor(proc)
        return proc

    monkeypatch.setattr(env, "_run_bash", spawn_with_high_stdout)
    code = f"print('x' * {length})"
    result = env.execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", timeout=5)

    assert result["returncode"] == 0
    assert result["output"].strip() == "x" * length


@pytest.mark.platforms("posix")
def test_background_terminal_captures_stdout_with_high_descriptor(tmp_path, move_stdout_to_high_descriptor):
    from tools.process_registry import ProcessRegistry

    with subprocess.Popen(
        [sys.executable, "-c", "print('background output')"],
        stdout=subprocess.PIPE, encoding="utf-8", errors="replace",
    ) as proc:
        move_stdout_to_high_descriptor(proc)
        registry = ProcessRegistry()
        session = registry.adopt_local(
            proc, command="print background output", cwd=str(tmp_path), notify_on_complete=False,
        )
        result = registry.wait(session.id, timeout=5)

    assert result["exit_code"] == 0
    assert result["output"].strip() == "background output"


class TestTimeoutPreservesPartialOutput:
    """When a command times out, any output captured before the deadline
    should be included in the result — not discarded."""

    def test_timeout_includes_partial_output(self):
        """A command that prints then sleeps past the deadline should
        return both the printed text and the timeout notice."""
        env = LocalEnvironment()
        result = env.execute("echo 'hello from test' && sleep 30", timeout=2)

        assert result["returncode"] == 124
        assert "hello from test" in result["output"]
        assert "timed out" in result["output"].lower()
