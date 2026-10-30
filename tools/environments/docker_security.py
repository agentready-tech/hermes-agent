"""Chromium namespace support for the shipped desktop image, without disabling seccomp."""

import hashlib
import json
from pathlib import Path


_CHROMIUM_PROFILE = Path(__file__).parent / "seccomp" / "chromium-seccomp.json"


def desktop_image(image: str) -> bool:
    """Only the known desktop tag opts into the additional namespace syscalls."""
    name = image.split("@", 1)[0]
    return name.removeprefix("docker.io/").removeprefix("index.docker.io/") == "nousresearch/hermes-sandbox:desktop"


def desktop_seccomp_args(image: str, extra_args: list[str]) -> list[str]:
    """Keep an operator's explicit seccomp policy; unrelated security options still compose."""
    if not desktop_image(image):
        return []
    for i, arg in enumerate(extra_args):
        if arg == "--security-opt" and i + 1 < len(extra_args):
            value = extra_args[i + 1]
        elif arg.startswith("--security-opt="):
            value = arg.partition("=")[2]
        else:
            continue
        if value.startswith(("seccomp=", "seccomp:")):
            return []
    if not _CHROMIUM_PROFILE.is_file():
        raise RuntimeError("The bundled Chromium seccomp profile is missing; repair the Hermes installation")
    return ["--security-opt", f"seccomp={_CHROMIUM_PROFILE}"]


def desktop_security_identity(image: str, extra_args: list[str]) -> str:
    """Security options are immutable: don't reuse a pre-profile container or stale VPN permissions.

    Hash the bundled policy contents, not its installation path (sealed environments move on update).
    Operator arguments are hashed, never exposed in a Docker label.
    """
    if not desktop_image(image):
        return ""
    policy = hashlib.sha256(_CHROMIUM_PROFILE.read_bytes()).hexdigest()
    payload = json.dumps({"policy": policy, "extra_args": extra_args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
