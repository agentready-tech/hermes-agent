"""Known UI bypasses are refused without running user source or UI tools."""
import json
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("source", [
    "xdotool type example", "/usr/bin/xdotool click 1", "env DISPLAY=:1 xdotool key A",
    "echo ready; xdotool mousemove 10 10", "import -window root /tmp/capture.png", "magick import -window root /tmp/capture.png",
    "python3 -c 'import ctypes; ctypes.CDLL(\"libXtst.so.6\")'",
    "python3 - <<'PY'\nimport ctypes\nx = ctypes.CDLL('libXtst.so.6')\nx.XTestFakeKeyEvent(1, 2, 3, 4)\nPY",
    "bash -c 'xdotool key A'", "bash -lc 'xdotool key A'", "echo ready\nxdotool key A",
    "DISPLAY=:1 XAUTHORITY=/own/auth xdotool click 1",
    "sudo -u pn -g pn xdotool click 1",
    "sudo -u pn python3 -c 'import ctypes; ctypes.CDLL(\"libXtst.so.6\")'",
])
def test_direct_shell_ui_forms_refused(source):
    from tools.desktop_ui_guard import desktop_ui_refusal
    assert "computer_use" in desktop_ui_refusal(source)


@pytest.mark.parametrize("source", [
    "import ctypes\nx = ctypes.CDLL('libc.so.6')\nx.XTestFakeKeyEvent(1, 2, 3, 4)",
    "import ctypes as c\nx = c.CDLL('libXtst.so.6')",
    "from pyautogui import click as press_mouse\npress_mouse(1, 2)",
    "from PIL import ImageGrab\nImageGrab.grab()",
    "import os\nos.system('xdotool click 1')",
    "import subprocess as sp\nsp.run(['xdotool', 'key', 'A'])",
])
def test_python_ui_calls_and_literal_subprocesses_refused(source):
    from tools.desktop_ui_guard import desktop_ui_refusal
    assert "computer_use" in desktop_ui_refusal(source, language="python")


@pytest.mark.parametrize("source,language", [
    ("rg 'XTestFakeKeyEvent|libXtst|xdotool' tools", "shell"),
    ("cat /tmp/helper.py", "shell"), ("echo xdotool", "shell"),
    ("python3 -m pytest tests/tools/test_desktop_ui_guard.py", "shell"),
    ("python3 -c 'print(\"XTestFakeKeyEvent xdotool\")'", "shell"),
    ("cat > /tmp/helper.py <<'PY'\nlib.XTestFakeKeyEvent(1)\nPY", "shell"),
    ("cat > /tmp/helper.sh <<'SH'\nxdotool click 1\nSH", "shell"),
    ("import ctypes\nc = ctypes.CDLL('libc.so.6')\nprint(c.strlen(b'example'))", "python"),
    ("# XTestFakeKeyEvent\nprint('xdotool libXtst')", "python"),
    ("DISPLAY=:1 XAUTHORITY=/own/auth rg 'xdotool|XTestFakeKeyEvent' tools", "shell"),
    ("sudo -u pn cat /own/helper.py", "shell"),
    ("sudo -u pn python3 -c 'import ctypes; ctypes.CDLL(\"libc.so.6\")'", "shell"),
])
def test_non_ui_code_source_reads_and_literals_preserved(source, language):
    from tools.desktop_ui_guard import desktop_ui_refusal
    assert desktop_ui_refusal(source, language=language) is None


@pytest.mark.parametrize("command", ["python3 /own/helper.py", "bash /own/helper.sh", "/own/helper.py"])
def test_literal_entry_helpers_are_inspected_boundedly(command):
    from tools.desktop_ui_guard import desktop_ui_refusal
    reader = Mock(return_value="x.XTestFakeKeyEvent(1)" if command.endswith(".py") else "xdotool key A")
    assert "computer_use" in desktop_ui_refusal(command, read_script=reader)
    assert reader.call_count == 1
    reader.return_value = "print('normal data processing')" if command.endswith(".py") else "printf ready"
    assert desktop_ui_refusal(command, read_script=reader) is None


@pytest.mark.parametrize("command", ["python3 /own/helper.sh", "python3 -W ignore /own/helper"])
def test_interpreter_owns_entry_language_not_filename(command):
    from tools.desktop_ui_guard import desktop_ui_refusal
    reader = Mock(return_value="x.XTestFakeKeyEvent(1)")
    assert "computer_use" in desktop_ui_refusal(command, read_script=reader)
    assert reader.call_count == 1


def test_backend_reader_is_bounded_and_never_reads_operator_files():
    from tools.desktop_ui_guard import MAX_SCRIPT_BYTES, read_backend_script
    env = Mock()
    env.execute.return_value = {"returncode": 0, "output": "x.XTestFakeKeyEvent(1)"}
    assert read_backend_script(env, "/own", "helper.py")
    command = env.execute.call_args.args[0]
    assert command == f"/usr/bin/head -c {MAX_SCRIPT_BYTES + 1} < /own/helper.py"
    assert env.execute.call_args.kwargs == {"timeout": 10}
    for output in ("x" * (MAX_SCRIPT_BYTES + 1), "binary\x00payload"):
        env.execute.return_value = {"returncode": 0, "output": output}
        assert read_backend_script(env, "/own", "helper.py") is None
    env.execute.side_effect = OSError("synthetic missing backend")
    assert read_backend_script(env, "/own", "helper.py") is None


@pytest.mark.parametrize("force", [False, True])
def test_terminal_refuses_before_environment_or_process_even_with_force(monkeypatch, force):
    from tools import terminal_tool as tt
    plan = Mock(side_effect=AssertionError("No execution planning or environment acquisition"))
    monkeypatch.setattr(tt, "_plan_execution", plan)
    result = json.loads(tt.terminal_tool("xdotool click 1", force=force))
    assert "computer_use" in result["error"]
    assert result["status"] == "blocked"
    plan.assert_not_called()


def test_terminal_preexec_refuses_own_entry_script_before_user_execution(monkeypatch):
    from tools import terminal_tool as tt
    env = Mock(cwd="/own")
    env.execute.return_value = {"returncode": 0, "output": "import ctypes\nx=ctypes.CDLL('libXtst.so.6')\nx.XTestFakeKeyEvent(1)"}
    monkeypatch.setattr(tt, "_resolve_command_cwd", lambda **_kw: "/own")
    lifecycle = Mock(side_effect=AssertionError("Must refuse before lifecycle probe/user execution"))
    monkeypatch.setattr(tt, "gateway_lifecycle_block", lifecycle)
    with pytest.raises(tt._Rejected) as exc:
        tt._pre_exec_block("python3 /own/helper.py", env=env, env_type="ssh", cwd="/own", workdir=None, session_key="synthetic")
    assert "computer_use" in str(exc.value)
    assert env.execute.call_count == 1
    assert env.execute.call_args.args[0].startswith("/usr/bin/head -c ")
    lifecycle.assert_not_called()


def test_execute_code_refuses_before_kernel_or_backend(monkeypatch):
    from tools import code_execution_tool as code
    backend = Mock(side_effect=AssertionError("No backend or kernel for refused code"))
    monkeypatch.setattr(code, "_get_or_create_env", backend)
    result = json.loads(code._execute_code("x.XTestFakeKeyEvent(1)"))
    assert result["code"] == "desktop_ui_requires_computer_use"
    assert "computer_use" in result["error"]
    backend.assert_not_called()
