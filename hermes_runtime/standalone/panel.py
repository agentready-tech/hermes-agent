"""Outbound Panel transport. Native display RPCs, tickets and leases remain authoritative."""

import asyncio
import json
import logging
from contextlib import suppress
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
READ_METHODS = {"display.status", "display.observe"}
CONTROL_METHODS = {"display.start", "display.lease.acquire", "display.lease.release"}


def panel_url(value):
    url = urlsplit(value.rstrip("/"))
    local = url.hostname in {"127.0.0.1", "localhost", "::1"}
    if url.scheme != "https" and not (local and url.scheme == "http"):
        raise ValueError("Panel URL requires HTTPS (HTTP is allowed only on localhost)")
    if not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise ValueError("Use the Panel origin, without a path or credentials")
    return urlunsplit((url.scheme, url.netloc, "", "", ""))


def socket_url(origin, environment_id, action, query=""):
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", environment_id):
        raise ValueError("Invalid Panel environment ID")
    url = urlsplit(panel_url(origin))
    return urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
        f"/app/api/environments/{environment_id}/computer/{action}", query, ""))


def enroll(home, origin, environment_id, token):
    """Persist recoverable local intent before consuming the one-time enrollment token."""
    import io
    from pathlib import Path
    from dotenv import set_key
    from hermes_yaml import roundtrip_yaml
    from utils import atomic_write_text

    origin = panel_url(origin)
    socket_url(origin, environment_id, "agent")  # validate the ID before the HTTP side effect
    if not token.strip():
        raise ValueError("Panel enrollment token is empty")
    path = Path(home) / "config.yaml"
    yaml = roundtrip_yaml()
    config = yaml.load(path.read_text())
    panel = config["autonomous"].setdefault("panel", {})
    previous_environment = panel.get("environment_id")
    panel.update({"url": origin, "environment_id": environment_id})
    stream = io.StringIO()
    yaml.dump(config, stream)
    atomic_write_text(path, stream.getvalue(), mode=0o600)
    # A credential selected locally before enrollment can be retried after a lost response.
    from dotenv import dotenv_values
    import secrets
    secret_file = Path(home) / ".env"
    credential = dotenv_values(secret_file).get("HERMES_PANEL_CREDENTIAL") if previous_environment == environment_id else None
    credential = credential or secrets.token_urlsafe(32)
    set_key(str(secret_file), "HERMES_PANEL_CREDENTIAL", credential)
    secret_file.chmod(0o600)
    request = Request(f"{origin}/app/api/environments/{environment_id}/computer/enroll",
        data=json.dumps({"enrollmentToken": token.strip(), "credential": credential}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("environmentId") != environment_id:
        raise ValueError("Panel returned a different environment ID")
    return {"environment_id": environment_id, "panel": origin}


def screen_request(raw):
    """Keep the machine connection screen-only even if a client bypasses Panel's UI."""
    message = json.loads(raw)
    if not isinstance(message, dict) or message.get("method") not in READ_METHODS | CONTROL_METHODS:
        raise ValueError("Only Bot Screen methods are permitted")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("Screen RPC params must be an object")
    allowed = {}
    if message["method"] in {"display.observe", "display.lease.acquire", "display.lease.release"}:
        viewer = params.get("viewer_id")
        if isinstance(viewer, str):
            allowed["viewer_id"] = viewer
    message["params"] = allowed
    return json.dumps(message)


async def bridge(command, config, port, credential, dashboard_token):
    from urllib.parse import urlencode
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    kind, identity = command.get("kind"), command.get("id")
    if kind not in {"rpc", "rfb"} or not isinstance(identity, str) or len(identity) > 100:
        raise ValueError("Invalid Panel channel")
    if kind == "rpc":
        target = f"ws://127.0.0.1:{port}/api/ws?" + urlencode({"token": dashboard_token})
    else:
        ticket = command.get("ticket")
        if not isinstance(ticket, str) or len(ticket) > 4096:
            raise ValueError("Invalid native display ticket")
        target = f"ws://127.0.0.1:{port}/api/display/ws?" + urlencode({"display_ticket": ticket})
    remote_url = socket_url(config["url"], config["environment_id"], "reverse", urlencode({"id": identity}))
    options = {"max_size": 4 * 1024 * 1024, "open_timeout": 15, "close_timeout": 5, "proxy": None}
    async with connect(target, **options) as native, connect(remote_url,
            additional_headers={"Authorization": f"Bearer {credential}"}, **options) as remote:
        async def pump(source, destination, inbound):
            try:
                async for frame in source:
                    if kind == "rfb" and not isinstance(frame, bytes):
                        raise ValueError("RFB must be binary")
                    if kind == "rpc" and inbound:
                        if not isinstance(frame, str) or len(frame) > 16384:
                            raise ValueError("Invalid screen RPC frame")
                        frame = screen_request(frame)
                    await destination.send(frame)
            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd else 1012
                await destination.close(code=code if code in {1000, 1001, 4000, 4001, 4401} else 1012)
            else:
                await destination.close(code=1000)
        tasks = [asyncio.create_task(pump(remote, native, True)), asyncio.create_task(pump(native, remote, False))]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Cancellation / a lost control connection must not look like an intentional hand-back.
            await native.close(code=1012)
            await remote.close(code=1012)


async def connection(config, port, credential, dashboard_token):
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException
    tasks = set()
    url = socket_url(config["url"], config["environment_id"], "agent")
    async with connect(url, additional_headers={"Authorization": f"Bearer {credential}"},
                       max_size=4096, open_timeout=15, close_timeout=5, proxy=None) as control:
        logger.info("Computer connected to Panel")
        async def open_channel(command):
            try:
                await bridge(command, config, port, credential, dashboard_token)
            except (OSError, ValueError, WebSocketException, TimeoutError):
                # Native tickets and credentials may occur in exception URLs; never log them.
                logger.warning("Panel screen channel ended; reconnect from the Panel if needed")
                with suppress(Exception):
                    await control.send(json.dumps({"type": "failed", "id": command.get("id")}))
        try:
            async for raw in control:
                command = json.loads(raw)
                if command.get("type") != "open" or len(tasks) >= 32:
                    continue
                task = asyncio.create_task(open_channel(command))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def worker(home):
    from websockets.exceptions import WebSocketException
    from agent.secret_scope import get_secret
    from hermes_runtime.standalone.config import settings
    from hermes_runtime.standalone.computer import start_desktop
    config = settings(home)
    panel = config.get("panel", {})
    credential = get_secret("HERMES_PANEL_CREDENTIAL") or ""
    dashboard_token = get_secret("HERMES_DASHBOARD_SESSION_TOKEN") or ""
    if not panel.get("url") or not credential or not dashboard_token:
        raise ValueError("Panel connection is not registered for this Hermes home")
    if config.get("computer", {}).get("enabled"):
        await asyncio.to_thread(start_desktop)
    delay = 1
    while True:
        try:
            await connection(panel, config["port"], credential, dashboard_token)
            delay = 1
        except (OSError, ValueError):
            logger.warning("Panel unavailable; the agent and its desktop continue locally")
        except (WebSocketException, TimeoutError):
            logger.warning("Panel connection ended; reconnecting without restarting the agent")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


def run(home):
    asyncio.run(worker(home))
