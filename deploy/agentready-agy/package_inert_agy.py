"""Verify, install and record the pinned inert Linux payload without executing it."""

import hashlib
import json
from pathlib import Path
import sys
import tarfile

PIN = Path(__file__).with_name("agy_linux_amd64.json")
DESTINATION = Path("/opt/hermes/inert/antigravity/agy")


def install(archive, output, pin):
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha512").hexdigest() != pin["sha512"]:
            raise ValueError("Antigravity archive does not match pin")
    with tarfile.open(archive, "r:gz") as package:
        members = [entry for entry in package if Path(entry.name).name == "antigravity"]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("Expected one regular Antigravity binary")
        stream = package.extractfile(members[0])
        if stream is None:
            raise ValueError("Antigravity binary missing")
        executable = stream.read()
    if hashlib.sha256(executable).hexdigest() != pin["executableSha256"]:
        raise ValueError("Antigravity binary does not match pin")
    if not executable.startswith(b"\x7fELF\x02\x01") or executable[18:20] != b"\x3e\x00":
        raise ValueError("Expected Linux x86-64 ELF")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(executable)
    output.chmod(0o555)



def main():
    archive, provenance = map(Path, sys.argv[1:])
    pin = json.loads(PIN.read_text())
    install(archive, DESTINATION, pin)
    settings = PIN.with_name("agy_offline_settings.json").read_bytes()
    policy = DESTINATION.with_name("settings.json")
    policy.write_bytes(settings)
    policy.chmod(0o444)
    DESTINATION.parent.chmod(0o555)
    DESTINATION.parent.parent.chmod(0o555)
    data = json.loads(provenance.read_text())
    data["inert_payloads"] = {"antigravity": {**pin, "path": str(DESTINATION), "settingsSha256": hashlib.sha256(settings).hexdigest(), "live": False}}
    data["capabilities"] = [name for name in data["capabilities"] if name != "gemini-antigravity-v1"]
    provenance.write_text(json.dumps(data) + "\n")
    provenance.chmod(0o444)


if __name__ == "__main__":
    main()
