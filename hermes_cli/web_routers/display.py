"""``/api/display/ws`` — raw RFB over WebSocket for the Bot Desktop viewer.

The Desktop renderer calls ``display.observe`` on its authenticated ``/api/ws`` connection, gets a
single-use 30 s ticket pinned to that profile's RFB socket, then opens this route with
``?display_ticket=``. No websockify, no new port: the bridge splices the profile's 0600 Unix socket
into the WebSocket as binary frames with backpressure both ways, and runs the client stream through
:class:`tools.bot_desktop.rfb_filter.RfbClientFilter` so keyboard, pointer and clipboard reach Xvnc
only from the viewer that currently holds the lease. noVNC's ``viewOnly`` is UX; this is the gate.

A lease change closes the evicted viewer's socket with 4000 ``control-taken`` so its UI drops back to
Watch mode and reconnects.

Design notes. The ticket rides in the query string on purpose: noVNC's Websock owns the socket and
cannot negotiate a subprotocol or add a header, and the ticket is single-use and expires in 30 s, so
a logged URL is spent by the time anyone reads it. The input gate re-reads the lease before each
client frame, including expiry, so a stale holder cannot keep typing after control returns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hermes_cli.web_server_chat import _ws_request_is_allowed

_log = logging.getLogger(__name__)
router = APIRouter()

_ACTIVITY_STAMP_S = 60.0
_READ_CHUNK = 64 * 1024
_CLOSE_CONTROL_TAKEN = 4000
_CLEAN_CLOSE = frozenset({1000, 1001})
_CLOSE_DESKTOP_GONE = 4001
_CLOSE_BAD_TICKET = 4401
_CLOSE_NOT_ALLOWED = 4403
_CLOSE_PROTOCOL = 1003
_WINDOW_CHANGE_REASONS = {45:"browser-window-geometry-changed",46:"browser-window-target-changed"}
_expiry_tasks: set[asyncio.Task] = set()
# Viewer capabilities belong to one RPC transport; its latest RFB stream supersedes old
# sockets in this single native API instance. The file lease remains control authority.
@dataclass
class _ViewerStreams:
    issued: int = 0
    active: Optional[int] = None
    opened: int = 0
    opening: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock,repr=False)


_viewer_streams: dict[tuple[str, str], _ViewerStreams] = {}


def connected_viewer_generation(viewer_id: str) -> Optional[int]:
    """Own-profile live RFB generation, never a reconnecting or already closed stream."""
    from hermes_constants import hermes_home_key
    streams = _viewer_streams.get((hermes_home_key(), viewer_id))
    return streams.active if streams else None


def _expire_disconnected_viewer(profile_home: str, viewer_id: str, epoch: int) -> None:
    """One bounded cleanup on the existing server loop, not a new sweeper or service."""
    from tools.bot_desktop import lease as _lease

    async def _expire() -> None:
        await asyncio.sleep(_lease.RECONNECT_GRACE_S)
        try:
            _lease.expire(profile_home, viewer_id=viewer_id, expected_epoch=epoch)
        except OSError:
            _log.warning("display lease expiry unavailable")

    task = asyncio.create_task(_expire())
    _expiry_tasks.add(task)
    task.add_done_callback(_expiry_tasks.discard)


def _should_evict(held: dict, lease, viewer_id: str) -> bool:
    """A viewer that held control during this connection and lost it to ANOTHER human is kicked so its
    UI repaints; a plain hand-back to the agent, pure watchers and the new holder stay connected. The
    hand-back also forgets that this viewer ever held: after it, they are a plain watcher again and a
    later takeover by someone else must not evict them. ``held`` is the per-connection memory."""
    from tools.bot_desktop import lease as _lease
    if lease.holder != _lease.HUMAN:
        held["ever"] = False
        return False
    if lease.viewer_id == viewer_id:
        held["ever"] = True
        return False
    return bool(held["ever"])


def _consume_display_ticket(ws: WebSocket) -> Optional[dict]:
    from hermes_cli.dashboard_auth.ws_tickets import TicketInvalid, consume_ticket
    ticket = ws.query_params.get("display_ticket", "")
    if not ticket:
        return None
    try:
        info = consume_ticket(ticket)
    except TicketInvalid:
        return None
    if info.get("provider") != "bot-desktop" or not info.get("hermes_home"):
        return None
    surface=info.get("surface")
    if surface not in (None,"desktop","browser-window"):
        return None
    if surface=="browser-window" and (type(info.get("lease_epoch")) is not int
            or not isinstance(info.get("window_nonce"),str) or len(info["window_nonce"])!=48
            or not isinstance(info.get("viewer_id"),str)):
        return None
    return info


@router.websocket("/api/display/ws")
async def display_ws(ws: WebSocket) -> None:
    # Host/Origin/client-IP policy is refused BEFORE accept, like every other dashboard route: a
    # cross-origin page never gets a completed handshake. Everything after that (ticket, desktop
    # state) is refused AFTER accept so the code + reason arrive in a close frame — a close before
    # accept surfaces in the browser as an opaque HTTP 403 and the renderer cannot tell "re-observe"
    # (4401) from "screen is gone" (4001).
    if not _ws_request_is_allowed(ws):
        await ws.close(code=_CLOSE_NOT_ALLOWED)
        return
    await ws.accept()
    info = _consume_display_ticket(ws)
    if info is None:
        await ws.close(code=_CLOSE_BAD_TICKET, reason="display ticket missing, expired or used")
        return
    await _bridge(ws, info)


async def _open_rfb(profile_home: Path):
    """``(reader, writer, relay)`` for THIS profile's Xvnc. Gateway-hosted screen: its unix socket. Screen inside
    the terminal backend: a ``docker exec`` / ``ssh`` relay whose stdio IS the RFB stream (``relay`` is that
    Popen; None for a socket). Raises OSError when nothing is running."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.bot_desktop import runtime as _bd_runtime
    token = set_hermes_home_override(profile_home)
    try:
        if _bd_runtime.sandbox_screen_running():
            relay = _bd_runtime.open_rfb_stream()
        else:
            sock = profile_home / "bot-desktop" / "rfb.sock"
            if not sock.exists():
                raise OSError("rfb.sock missing")
            reader, writer = await asyncio.open_unix_connection(str(sock))
            return reader, writer, None
    finally:
        reset_hermes_home_override(token)
    return await _relay_io(relay)


async def _relay_io(relay):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), relay.stdout)
    # StreamReaderProtocol (not the bare FlowControlMixin) so ``writer.wait_closed()`` in the bridge's teardown has
    # a close waiter to await; on the bare mixin it raises NotImplementedError.
    w_transport, w_protocol = await loop.connect_write_pipe(lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()), relay.stdin)
    writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)
    return reader, writer, relay


async def _finish_browser_view(info, commit):
    from tui_gateway.server import _session_profile_runtime_scope
    from tools.bot_desktop import browser_window
    def finish():
        with _session_profile_runtime_scope({"profile_home":info["hermes_home"]},hydrate_secrets=False):
            browser_window.finish(info["viewer_id"],info["lease_epoch"],info["window_nonce"],commit=commit)
    await asyncio.to_thread(finish)


async def _open_browser_rfb(info):
    from tools.bot_desktop import browser_window, lease
    from tui_gateway.server import _session_profile_runtime_scope
    def owned_open():
        with _session_profile_runtime_scope({"profile_home":info["hermes_home"]},hydrate_secrets=False):
            return browser_window.open_stream(info["viewer_id"],info["lease_epoch"],info["window_nonce"])
    relay=None;writer=None
    try:
        relay,geometry=await asyncio.to_thread(owned_open)
        reader,writer,_=await _relay_io(relay)
        raw=await asyncio.wait_for(reader.readline(),timeout=15)
        import json
        ready=json.loads(raw) if len(raw)<=512 else {}
        current=lease.get(info["hermes_home"])
        if (ready!={"ok":True,"geometry":geometry} or current.holder!=lease.HUMAN
                or current.viewer_id!=info["viewer_id"] or current.epoch!=info["lease_epoch"]):
            raise OSError("browser_window_unavailable")
        return reader,writer,relay
    except BaseException as error:
        if writer is not None:writer.close()
        if relay is not None:relay.kill()
        if isinstance(error,(ValueError,RuntimeError,TimeoutError)):
            raise OSError("browser_window_unavailable") from None
        raise


async def _bridge(ws: WebSocket, info: dict) -> None:
    """Pump RFB bytes between the viewer socket (already accepted) and THIS profile's Xvnc, gated by
    the lease."""
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import lease as _lease
    from tools.bot_desktop.rfb_filter import RfbClientFilter

    profile_home = str(info["hermes_home"])
    profile_key = hermes_home_key(profile_home)
    viewer_id = str(info.get("viewer_id") or info.get("user_id") or "viewer")
    stream_key = (profile_key, viewer_id)
    streams = _viewer_streams.setdefault(stream_key, _ViewerStreams())
    with streams.lock:
        streams.issued += 1
        stream_token = streams.issued
        streams.opening += 1

    def _owns_stream() -> bool:
        return _viewer_streams.get(stream_key) is streams and streams.active == stream_token

    def _commit_finish(action, *, failed=False):
        with streams.lock:
            current=_viewer_streams.get(stream_key) is streams and streams.issued==stream_token and not streams.opening
            valid=current and (streams.active is None if failed else streams.active==stream_token)
            return action() if valid else False

    def _forget_stream() -> None:
        with streams.lock:
            if _owns_stream():streams.active=None
            if streams.active is None and not streams.opening and _viewer_streams.get(stream_key) is streams:
                _viewer_streams.pop(stream_key,None)

    try:
        reader, writer, relay = (await _open_browser_rfb(info) if info.get("surface")=="browser-window"
                                 else await _open_rfb(Path(profile_home)))
    except OSError as exc:
        with streams.lock:streams.opening -= 1
        if info.get("surface")=="browser-window":await _finish_browser_view(info,lambda action:_commit_finish(action,failed=True))
        _forget_stream()
        _log.warning("display ws: cannot reach RFB endpoint for %s: %s", profile_home, exc)
        await ws.close(code=_CLOSE_DESKTOP_GONE, reason=("browser-window-unavailable"
                       if info.get("surface")=="browser-window" else "Bot Desktop is not running"))
        return
    except BaseException:
        with streams.lock:streams.opening -= 1
        _forget_stream()
        raise
    with streams.lock:
        streams.opening -= 1
        if stream_token > streams.opened:
            streams.opened, streams.active = stream_token, stream_token

    loop = asyncio.get_running_loop()
    evicted = asyncio.Event()
    current = _lease.get(profile_home)
    held = {"ever": current.holder == _lease.HUMAN and current.viewer_id == viewer_id}
    allowed = {"input": held["ever"]}
    last_held_epoch = {"value": current.epoch if held["ever"] else None}
    disconnect_epoch = {"value": None}

    def _refresh_allowed(lease=None) -> None:
        if not _owns_stream():
            allowed["input"] = False
            return
        if lease is None:
            lease = _lease.get(profile_key=profile_home)
        allowed["input"] = (lease.holder == _lease.HUMAN and lease.viewer_id == viewer_id
                            and (info.get("surface")!="browser-window" or lease.epoch==info["lease_epoch"]))
        if allowed["input"]:
            last_held_epoch["value"] = lease.epoch

    def _may_send_input() -> bool:
        _refresh_allowed()
        return allowed["input"]

    def _on_lease(key: str, lease) -> None:
        if key != profile_key:
            return
        if not _owns_stream():
            loop.call_soon_threadsafe(evicted.set)
            return
        loop.call_soon_threadsafe(_refresh_allowed, lease)
        if info.get("surface")=="browser-window" and (lease.holder!=_lease.HUMAN
                or lease.viewer_id!=viewer_id or lease.epoch!=info["lease_epoch"]):
            loop.call_soon_threadsafe(evicted.set)
        if _should_evict(held, lease, viewer_id):
            loop.call_soon_threadsafe(evicted.set)
    unsubscribe = _lease.on_change(_on_lease)

    rfb_filter = RfbClientFilter(_may_send_input)

    viewer_closed = asyncio.Event()
    view_change = {"value":False}
    activity_file = Path(profile_home) / "bot-desktop" / "activity"
    stamped = {"at": 0.0}

    def _stamp_activity() -> None:
        # An attached viewer is use: the idle auto-stop must not take a screen someone is watching.
        if loop.time() - stamped["at"] < _ACTIVITY_STAMP_S:
            return
        stamped["at"] = loop.time()
        try:
            activity_file.touch()
        except OSError:
            pass

    async def rfb_to_ws() -> None:
        while True:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                if info.get("surface")=="browser-window" and relay is not None:
                    import subprocess
                    try:code=await asyncio.to_thread(relay.wait,timeout=2)
                    except subprocess.TimeoutExpired:code=None
                    reason=_WINDOW_CHANGE_REASONS.get(code,"browser-window-unavailable")
                    view_change["value"]=code in _WINDOW_CHANGE_REASONS
                    await ws.close(code=_CLOSE_DESKTOP_GONE,reason=reason)
                return
            _stamp_activity()
            await ws.send_bytes(chunk)  # awaiting the send is the backpressure toward Xvnc

    async def ws_to_rfb() -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                # Capture before async teardown: an old close must not release a newer
                # acquisition, even when the same viewer reconnected in the meantime.
                disconnect_epoch["value"] = last_held_epoch["value"]
                # 1000/1001 = the viewer closed the window; anything else is a dropped link.
                if message.get("code") in _CLEAN_CLOSE:
                    viewer_closed.set()
                return
            data = message.get("bytes")
            if data is None:
                await ws.close(code=_CLOSE_PROTOCOL, reason="RFB is binary")
                return
            try:
                allowed = rfb_filter.feed(data)
            except ValueError as exc:
                await ws.close(code=_CLOSE_PROTOCOL, reason=str(exc)[:100])
                return
            if allowed:
                writer.write(allowed)
                await writer.drain()  # backpressure toward the browser

    async def watch_eviction() -> None:
        await evicted.wait()
        await ws.close(code=_CLOSE_CONTROL_TAKEN, reason="control-taken")

    async def renew_human_control() -> None:
        while _owns_stream():
            current = _lease.get(profile_home)
            if (current.holder == _lease.HUMAN and current.viewer_id == viewer_id
                    and (info.get("surface")!="browser-window" or current.epoch==info["lease_epoch"])):
                _lease.renew(viewer_id, expected_epoch=current.epoch, profile_key=profile_home)
                _refresh_allowed(current)
            await asyncio.sleep(_lease.RENEW_INTERVAL_S)

    tasks = [asyncio.create_task(rfb_to_ws()), asyncio.create_task(ws_to_rfb()),
             asyncio.create_task(watch_eviction()), asyncio.create_task(renew_human_control())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        # Cancelled pumps must finish before we tear down the socket they hold, or they outlive the
        # bridge on the loop (a viewer reconnecting in a loop piled them up).
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                _log.debug("display ws ended: %r", exc)
    finally:
        unsubscribe()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:  # Xvnc already gone (ECONNRESET / EPIPE on the FIN)
            pass
        if relay is not None:
            relay.kill()  # the exec client; Xvnc inside the sandbox stays up
        epoch = disconnect_epoch["value"]
        if epoch is None:
            epoch = last_held_epoch["value"]
        if epoch is not None and _owns_stream():
            if viewer_closed.is_set() and not view_change["value"]:
                if info.get("surface")=="browser-window":await _finish_browser_view(info,_commit_finish)
                else:_lease.release(viewer_id, profile_key=profile_home, expected_epoch=epoch)
            else:
                # Give a dropped viewer a short reconnect grace. Expiration is persisted
                # even if no later agent action reads the lease; restart uses its deadline.
                _expire_disconnected_viewer(profile_home, viewer_id, epoch)
        _forget_stream()
        try:
            await ws.close()
        except Exception:  # already closed by the peer or by an eviction
            pass
