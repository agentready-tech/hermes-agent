"""The executable boundary must remove agent-browser's container bypass, without losing argv."""

import json
import os
import subprocess

import pytest

from tools.bot_desktop.chromium_sandbox import launcher_script


@pytest.mark.platforms("posix")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="Launcher intentionally refuses root")
def test_launcher_preserves_browser_arguments_and_removes_the_dependency_bypass(tmp_path):
    executable = tmp_path / "browser with spaces"
    executable.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    executable.chmod(0o700)
    launcher = tmp_path / "chromium-sandbox"
    launcher.write_text(launcher_script(str(executable)))
    launcher.chmod(0o700)
    result = subprocess.run([str(launcher), "--no-sandbox", "--no-sandbox=true", "--headless=new",
                             "--user-data-dir=/a profile", "data:text/html,hello world"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["--headless=new", "--user-data-dir=/a profile", "data:text/html,hello world"]
