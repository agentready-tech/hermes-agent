"""Image children use the shipped interpreter instead of a missing source launcher."""

import json
import subprocess
import sys

import pytest

from hermes_cli import image_provenance
from hermes_runtime.standalone.service import _command


def test_image_command_runs_a_real_child_with_its_shipped_python(tmp_path, monkeypatch):
    marker = tmp_path / "image.json"
    marker.write_text(json.dumps({"schema": 1, "deployment_kind": "image", "manager": "docker"}))
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    source = tmp_path / "input.json"
    source.write_text('{"preserved":true}')
    command = _command("json.tool", str(source), interpreter=sys.executable, home=tmp_path)
    assert command[0] == sys.executable
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"preserved": True}


def test_invalid_image_marker_cannot_fall_back_to_a_source_install(tmp_path, monkeypatch):
    marker = tmp_path / "image.json"
    marker.write_text("invalid")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    with pytest.raises(ValueError, match="provenance marker is invalid"):
        _command("hermes_runtime")
