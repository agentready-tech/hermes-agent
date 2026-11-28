"""Validate the inert packaging verifier using a non-executable synthetic ELF header."""

import hashlib
import io
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest

from providers.agy_policy import BINARY


def _packaging():
    """Load the image-owned installer from its non-Python-package deploy directory."""
    path = Path(__file__).resolve().parents[2] / "deploy" / "agentready-agy" / "package_inert_agy.py"
    spec = importlib.util.spec_from_file_location("package_inert_agy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packaging = _packaging()


def test_pin_and_inert_package_verifier(tmp_path, monkeypatch):
    settings_file = packaging.PIN.with_name("agy_offline_settings.json")
    settings = json.loads(settings_file.read_text())
    assert settings["toolPermission"] == "strict" and settings["agentMode"] == "" and settings["useG1Credits"] is False
    assert "read_file(*)" in settings["permissions"]["deny"]
    pin = json.loads(packaging.PIN.read_text())
    assert pin["platform"] == "linux/amd64" and len(pin["sha512"]) == 128
    assert len(pin["executableSha256"]) == 64 and Path(BINARY) == packaging.DESTINATION

    payload = b"\x7fELF\x02\x01" + b"\x00" * 12 + b"\x3e\x00" + b"NOT AN EXECUTABLE"
    archive = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        info = tarfile.TarInfo("antigravity")
        info.size = len(payload)
        package.addfile(info, io.BytesIO(payload))
    output = tmp_path / "distribution" / "antigravity" / "agy"
    with pytest.raises(ValueError, match="archive does not match"):
        packaging.install(archive, output, pin)
    assert not output.exists()
    fixture = {**pin, "sha512": hashlib.sha512(archive.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match="binary does not match"):
        packaging.install(archive, output, fixture)
    assert not output.exists()
    fixture["executableSha256"] = hashlib.sha256(payload).hexdigest()
    fixture_pin = tmp_path / "pin.json"
    fixture_pin.write_text(json.dumps(fixture))
    fixture_pin.with_name(settings_file.name).write_bytes(settings_file.read_bytes())
    provenance = tmp_path / "provenance.json"
    provenance.write_text(json.dumps({"revision": "fixture-source", "capabilities": ["subscriptions-v1", "gemini-antigravity-v1"]}))
    monkeypatch.setattr(packaging, "PIN", fixture_pin)
    monkeypatch.setattr(packaging, "DESTINATION", output)
    monkeypatch.setattr("sys.argv", ["package_inert_agy.py", str(archive), str(provenance)])
    with pytest.raises(ValueError, match="cannot advertise live Gemini"):
        packaging.main()
    assert not output.exists()
    provenance.write_text(json.dumps({"revision": "fixture-source", "capabilities": ["subscriptions-v1"]}))
    packaging.main()
    installed = json.loads(provenance.read_text())
    assert installed["revision"] == "fixture-source" and installed["capabilities"] == ["subscriptions-v1"]
    assert installed["inert_payloads"]["antigravity"]["live"] is False
    assert installed["inert_payloads"]["antigravity"]["settingsSha256"] == hashlib.sha256(settings_file.read_bytes()).hexdigest()
    assert output.read_bytes() == payload and output.stat().st_mode & 0o222 == 0
    assert output.with_name("settings.json").stat().st_mode & 0o222 == 0
    # Make the fixture removable by a non-root test runner.
    output.parent.chmod(0o700)
    output.parent.parent.chmod(0o700)
