"""Read and update the owning employee's SOUL.md over the authenticated Panel link."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

CAPABILITY = "agent-instructions-v1"
MAX_CONTENT_BYTES = 32 * 1024
MAX_FRAME_BYTES = 256 * 1024
FILENAME = "SOUL.md"
CONFLICT = "CONFLICT_AGENT_INSTRUCTIONS"
INVALID = "INVALID_AGENT_INSTRUCTIONS"
UNAVAILABLE = "AGENT_INSTRUCTIONS_UNAVAILABLE"


def _encode(content: str) -> bytes:
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError(INVALID)
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        raise ValueError(INVALID) from None
    if len(raw) > MAX_CONTENT_BYTES:
        raise ValueError("AGENT_INSTRUCTIONS_TOO_LARGE")
    return raw


def _read(directory: int) -> dict:
    try:
        fd = os.open(FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return {"content": "", "revision": None}
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(UNAVAILABLE)
        raw = stream.read(MAX_CONTENT_BYTES + 1)
    if len(raw) > MAX_CONTENT_BYTES:
        raise ValueError("AGENT_INSTRUCTIONS_TOO_LARGE")
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError(UNAVAILABLE) from None
    if "\x00" in content:
        raise ValueError(UNAVAILABLE)
    return {"content": content, "revision": hashlib.sha256(raw).hexdigest()}


def _replace(directory: int, raw: bytes) -> None:
    temporary = ".panel-soul-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _update(directory: int, params: dict) -> dict:
    import fcntl

    if set(params) != {"content", "expectedRevision"}:
        raise ValueError(INVALID)
    expected = params["expectedRevision"]
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError(INVALID)
    raw = _encode(params["content"])
    revision = hashlib.sha256(raw).hexdigest()
    # Serialize writers without blocking the agent's own reads of the file.
    fcntl.flock(directory, fcntl.LOCK_EX)
    current = _read(directory)
    if current["revision"] == revision:
        return current  # A retry after a lost reply is already saved.
    if current["revision"] != expected:
        raise ValueError(CONFLICT)
    _replace(directory, raw)
    return {"content": params["content"], "revision": revision}


def instructions_request(home: Path, method: str, params: dict) -> dict:
    if not isinstance(params, dict) or not (
        (method == "instructions.read" and not params) or method == "instructions.save"
    ):
        raise ValueError(INVALID)
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _read(directory) if method == "instructions.read" else _update(directory, params)
    finally:
        os.close(directory)


async def _reply(home: Path, raw: str) -> dict:
    identity = None
    try:
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) != {"jsonrpc", "id", "method", "params"}
                or request["jsonrpc"] != "2.0" or type(request["id"]) not in {int, str}):
            raise ValueError(INVALID)
        identity = request["id"]
        result = await asyncio.to_thread(instructions_request, home, request["method"], request["params"])
    except (OSError, ValueError) as error:
        name = str(error) if isinstance(error, ValueError) else UNAVAILABLE
        if name not in {INVALID, CONFLICT, UNAVAILABLE, "AGENT_INSTRUCTIONS_TOO_LARGE"}:
            name = INVALID
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": 409 if name == CONFLICT else 400, "message": name}}
    return {"jsonrpc": "2.0", "id": identity, "result": result}


async def bridge_instructions(remote_url: str, credential: str) -> None:
    from websockets.asyncio.client import connect
    from hermes_runtime.adapters.hermes import managed_home

    home = managed_home()
    async with connect(remote_url, additional_headers={"Authorization": f"Bearer {credential}"},
                       max_size=MAX_FRAME_BYTES, open_timeout=15, close_timeout=5, proxy=None) as remote:
        async for raw in remote:
            if not isinstance(raw, str):
                await remote.close(1003)
                return
            await remote.send(json.dumps(await _reply(home, raw), ensure_ascii=False))
