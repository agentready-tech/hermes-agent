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


def screen_request(raw, *, can_control=False, can_configure=False, browser_only=False):
    """Keep the machine connection screen-only even if a client bypasses Panel's UI."""
    message = json.loads(raw)
    if not isinstance(message, dict) or message.get("method") not in READ_METHODS | CONTROL_METHODS | {"display.browser.google_login"}:
        raise ValueError("Only Bot Screen methods are permitted")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("Screen RPC params must be an object")
    if browser_only and message["method"] not in {"display.status","display.observe","display.browser.google_login","display.lease.release"}:
        raise ValueError("Only the own browser login surface is permitted")
    if message["method"]=="display.observe":
        surface=params.get("surface")
        if surface not in (None,"desktop","google-login","browser-window"):
            raise ValueError("Unknown display surface")
        if browser_only and surface not in ("google-login","browser-window"):
            raise ValueError("A Google login cannot expose the Desktop")
        if surface in ("google-login","browser-window"):
            if not can_control or not can_configure:raise ValueError("Browser login requires both permissions")
            if surface=="google-login":
                if set(params)-{"surface","viewport"}:raise ValueError("Invalid browser intent")
                from tools.bot_desktop.browser_window import validate_viewport
                validate_viewport(params.get("viewport"))
            elif (set(params)!={"surface","viewer_id"} or not isinstance(params["viewer_id"],str)
                    or not 1<=len(params["viewer_id"])<=128):raise ValueError("Invalid browser view")
            return json.dumps(message)
    if browser_only and message["method"]=="display.lease.release" and (
            set(params)!={"viewer_id"} or not isinstance(params["viewer_id"],str)):
        raise ValueError("An own viewer is required")
    if message["method"] == "display.browser.google_login":
        if (not can_control or not can_configure or set(params) != {"viewer_id", "expected_epoch"}
                or not isinstance(params["viewer_id"], str) or not 1 <= len(params["viewer_id"]) <= 128
                or type(params["expected_epoch"]) is not int or params["expected_epoch"] < 0):
            raise ValueError("Google browser sign-in requires own control and configuration permission")
        return json.dumps(message)
    allowed = {}
    if message["method"] in {"display.observe", "display.lease.acquire", "display.lease.release"}:
        viewer = params.get("viewer_id")
        if isinstance(viewer, str):
            allowed["viewer_id"] = viewer
    message["params"] = allowed
    return json.dumps(message)


async def bridge(command, config, port, credential, auth, subscriptions=None, google_auth=None):
    from urllib.parse import urlencode
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, WebSocketException

    kind, identity = command.get("kind"), command.get("id")
    if kind not in {"rpc", "rfb", "chat", "subscriptions"} or not isinstance(identity, str) or len(identity) > 100:
        raise ValueError("Invalid Panel channel")
    if command.get("googleLogin") is True:
        if (kind not in {"rpc", "rfb"} or command.get("canControl") is not True
                or command.get("canConfigure") is not True or google_auth is None
                or not getattr(google_auth, "gateway_owned", False)):
            raise ValueError("Own Google browser authority is unavailable")
        await asyncio.to_thread(google_auth.endpoint)
        auth = google_auth
    if kind == "subscriptions":
        if subscriptions is None:
            raise ValueError("Subscriptions unavailable")
        remote_url = socket_url(config["url"], config["environment_id"], "reverse", urlencode({"id": identity}))
        await subscriptions.bridge(remote_url, credential)
        return
    if kind == "chat":
        from hermes_runtime.standalone.panel_chat import gateway_chat_available, bridge_chat
        if auth.gateway_owned or gateway_chat_available():
            remote_url = socket_url(config["url"], config["environment_id"], "reverse", urlencode({"id": identity}))
            await bridge_chat(remote_url, credential, {"max_size": 32 * 1024 * 1024,
                "open_timeout": 15, "close_timeout": 5, "proxy": None})
            return
    native_options = {}
    if kind in {"rpc", "chat"}:
        target, native_options = await asyncio.to_thread(auth.rpc_connection)
    else:
        ticket = command.get("ticket")
        if not isinstance(ticket, str) or len(ticket) > 4096:
            raise ValueError("Invalid native display ticket")
        target = await asyncio.to_thread(auth.display_url, ticket)
    remote_url = socket_url(config["url"], config["environment_id"], "reverse", urlencode({"id": identity}))
    options = {"max_size": (32 if kind == "chat" else 4) * 1024 * 1024, "open_timeout": 15, "close_timeout": 5, "proxy": None}
    async with connect(target, **options, **native_options) as native, connect(remote_url,
            additional_headers={"Authorization": f"Bearer {credential}"}, **options) as remote:
        events = None
        if kind == "chat":
            # Panel authorizes and filters chat RPCs; Hermes keeps its existing chat backend.
            with suppress(OSError, WebSocketException, TimeoutError):
                event_url, event_options = await asyncio.to_thread(auth.rpc_connection, True)
                events = await connect(event_url, **options, **event_options)
        async def pump(source, destination, inbound):
            try:
                async for frame in source:
                    if kind == "rfb" and not isinstance(frame, bytes):
                        raise ValueError("RFB must be binary")
                    if kind == "rpc" and inbound:
                        if not isinstance(frame, str) or len(frame) > 16384:
                            raise ValueError("Invalid screen RPC frame")
                        frame = screen_request(frame, can_control=command.get("canControl") is True,
                                               can_configure=command.get("canConfigure") is True and command.get("googleLogin") is True,
                                               browser_only=command.get("googleLogin") is True)
                    if kind == "chat" and inbound and isinstance(frame, str):
                        # A Telegram binding can appear after a browser-only viewer attached.
                        # Reconnect onto its owner before accepting another legacy write.
                        from hermes_runtime.standalone.panel_chat import gateway_chat_available
                        if json.loads(frame).get("method") == "prompt.submit" and gateway_chat_available():
                            await destination.close(code=1012)
                            await source.close(code=1012)
                            return
                    await destination.send(frame)
            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd else 1012
                reason=error.rcvd.reason if error.rcvd else ""
                reason=reason if (not inbound and kind=="rfb" and command.get("googleLogin") is True
                    and code==4001 and reason in {"browser-window-target-changed","browser-window-geometry-changed"}) else ""
                await destination.close(code=code if code in {1000,1001,4000,4001,4401} else 1012,reason=reason)
            else:
                await destination.close(code=1000)
        tasks = [asyncio.create_task(pump(remote, native, True)), asyncio.create_task(pump(native, remote, False))]
        observer = None
        if events:
            async def observe():
                with suppress(ConnectionClosed):
                    async for frame in events:
                        await remote.send(frame)
            observer = asyncio.create_task(observe())
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
            if observer:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                await events.close()
            # Cancellation / a lost control connection must not look like an intentional hand-back.
            await native.close(code=1012)
            await remote.close(code=1012)


async def connection(config, port, credential, auth, subscriptions=None, google_auth=None):
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException
    tasks = set()
    url = socket_url(config["url"], config["environment_id"], "agent")
    async with connect(url, additional_headers={"Authorization": f"Bearer {credential}"},
                       max_size=4096, open_timeout=15, close_timeout=5, proxy=None) as control:
        logger.info("Computer connected to Panel")
        google_ready = False
        if google_auth is not None:
            try:
                await asyncio.to_thread(google_auth.endpoint)
                from tools.bot_desktop.browser_window import available
                google_ready = await asyncio.to_thread(available)
            except (OSError, ValueError):
                pass  # No intent capability until the existing own gateway is ready.
        await control.send(json.dumps({"type": "hello", "capabilities": ["screen", "chat", *(["subscriptions"] if subscriptions else []),
            *(["google-browser-window-v1"] if google_ready else [])]}))
        async def open_channel(command):
            try:
                await bridge(command, config, port, credential, auth, subscriptions, google_auth)
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


async def _worker(home):
    from hermes_runtime.adapters.hermes import activate
    activate(home)
    from websockets.exceptions import WebSocketException
    from agent.secret_scope import get_secret
    from hermes_runtime.standalone.config import dashboard_enabled, settings
    from hermes_runtime.standalone.computer import start_desktop
    from hermes_runtime.standalone.panel_auth import NativeScreenAuth
    config = settings(home)
    panel = config.get("panel", {})
    credential = get_secret("HERMES_PANEL_CREDENTIAL") or ""
    dashboard_token = get_secret("HERMES_DASHBOARD_SESSION_TOKEN") or ""
    with_dashboard = dashboard_enabled(config)
    if not panel.get("url") or not credential or (with_dashboard and not dashboard_token):
        raise ValueError("Panel connection is not registered for this Hermes home")
    if config.get("computer", {}).get("enabled"):
        await asyncio.to_thread(start_desktop)
    if with_dashboard:
        auth = NativeScreenAuth(config["port"], dashboard_token,
            username=get_secret("HERMES_DASHBOARD_BASIC_AUTH_USERNAME"),
            password=get_secret("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"))
    else:
        from hermes_runtime.standalone.panel_gateway_auth import GatewayPanelAuth
        auth = GatewayPanelAuth(home)
    from hermes_runtime.standalone.panel_subscriptions import Subscriptions
    subscriptions = Subscriptions(home, auth)
    from hermes_runtime.standalone.panel_gateway_auth import GatewayPanelAuth
    google_auth = GatewayPanelAuth(home)
    delay = 1
    while True:
        try:
            await connection(panel, config["port"], credential, auth, subscriptions, google_auth)
            delay = 1
        except (OSError, ValueError):
            logger.warning("Panel unavailable; the agent and its desktop continue locally")
        except (WebSocketException, TimeoutError):
            logger.warning("Panel connection ended; reconnecting without restarting the agent")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def worker(home):
    from hermes_runtime.standalone.sandbox_events import watch
    task = asyncio.create_task(watch(home))
    try:
        await _worker(home)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def run(home):
    asyncio.run(worker(home))
