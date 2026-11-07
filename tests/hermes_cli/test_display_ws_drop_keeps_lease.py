"""A dropped link retains control only during its reconnect grace; a clean close releases now.
Bounded drop expiry and active-viewer renewal are covered by test_display_lease_liveness.py."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from hermes_cli.web_routers import display
from tools.bot_desktop import lease


class _Ws:
    """Just enough of a Starlette WebSocket: one disconnect message with the given close code."""

    def __init__(self, close_code: int):
        self._code = close_code
        self.closed = False

    async def accept(self):
        pass

    async def receive(self):
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect", "code": self._code}

    async def send_bytes(self, data):
        pass

    async def close(self, code=1000, reason=""):
        self.closed = True


async def _bridge_once(close_code: int, home: str) -> lease.Lease:
    sock_dir = os.path.join(home, "bot-desktop")
    os.makedirs(sock_dir, exist_ok=True)
    sock = os.path.join(sock_dir, "rfb.sock")

    async def _xvnc(reader, writer):  # a silent framebuffer
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_unix_server(_xvnc, path=sock)
    try:
        info = {"hermes_home": home, "viewer_id": "desk-1"}
        await display._bridge(_Ws(close_code), info)
    finally:
        server.close()
    return lease.get(profile_key=home)


# 1005 (no status code) is what noVNC's code-less socket.close() AND some proxies produce on a drop, so
# the server keeps the lease for a short reconnect grace; Desktop intentional close sends1000.
@pytest.mark.parametrize(("close_code", "human_keeps_control"), [(1006, True), (1005, True), (1000, False)])
def test_only_a_clean_viewer_close_hands_the_screen_back(monkeypatch, close_code, human_keeps_control):
    lease._reset_for_tests()
    with tempfile.TemporaryDirectory() as home:
        lease.acquire("desk-1", profile_key=home)
        after = asyncio.run(_bridge_once(close_code, home))
    lease._reset_for_tests()
    assert (after.holder == lease.HUMAN) is human_keeps_control, after


def test_ex_holder_who_handed_back_is_not_evicted_by_a_later_takeover():
    """desk-1 holds, hands back to the agent, then desk-2 takes over: desk-1 is a plain watcher again
    and must stay connected; only a takeover WHILE desk-1 held (or believed it held) kicks it."""
    held = {"ever": False}
    assert display._should_evict(held, lease.Lease(holder=lease.HUMAN, viewer_id="desk-1"), "desk-1") is False
    assert display._should_evict(held, lease.Lease(holder=lease.AGENT), "desk-1") is False
    assert display._should_evict(held, lease.Lease(holder=lease.HUMAN, viewer_id="desk-2"), "desk-1") is False

    held = {"ever": False}
    display._should_evict(held, lease.Lease(holder=lease.HUMAN, viewer_id="desk-1"), "desk-1")
    assert display._should_evict(held, lease.Lease(holder=lease.HUMAN, viewer_id="desk-2"), "desk-1") is True


class _OpenWs(_Ws):
    """Stays open until ``finish`` is set, then reports a clean close."""

    def __init__(self):
        super().__init__(1000)
        self.finish = asyncio.Event()

    async def receive(self):
        await self.finish.wait()
        return {"type": "websocket.disconnect", "code": self._code}


def test_a_takeover_made_by_another_process_stops_input_before_the_next_frame(monkeypatch):
    """The authoritative frame gate also observes a transition made by another process."""
    from tools.bot_desktop import rfb_filter
    captured = {}

    class _Filter(rfb_filter.RfbClientFilter):
        def __init__(self, allow_input):
            super().__init__(allow_input)
            captured["allow"] = allow_input
            captured["ready"].set()
    monkeypatch.setattr(rfb_filter, "RfbClientFilter", _Filter)

    async def _run(home: str) -> None:
        sock_dir = os.path.join(home, "bot-desktop")
        os.makedirs(sock_dir, exist_ok=True)
        async def _xvnc(reader, writer):
            # Keep the synthetic RFB endpoint alive until the bridge closes its writer.
            await reader.read()
            writer.close()

        server = await asyncio.start_unix_server(_xvnc, path=os.path.join(sock_dir, "rfb.sock"))
        captured["ready"] = asyncio.Event()
        ws = _OpenWs()
        task = asyncio.create_task(display._bridge(ws, {"hermes_home": home, "viewer_id": "desk-1"}))
        try:
            await asyncio.wait_for(captured["ready"].wait(), 2)
            assert captured["allow"]() is True
            # Another process takes over: the file changes, no listener in this process is told.
            lease._write(lease._path(home), lease.Lease(holder=lease.HUMAN, viewer_id="desk-2", epoch=2))
            assert captured["allow"]() is False
        finally:
            ws.finish.set()
            await task
            server.close()

    lease._reset_for_tests()
    with tempfile.TemporaryDirectory() as home:
        lease.acquire("desk-1", profile_key=home)
        asyncio.run(_run(home))
    lease._reset_for_tests()


def test_no_bridge_task_or_socket_outlives_the_bridge():
    """The pumps were cancelled but never awaited and the Xvnc writer closed without wait_closed, so
    a returned _bridge still had tasks (and a half-closed socket) in flight on the loop; a viewer
    reconnecting in a tight loop accumulated them. Everything the bridge started is finished, and
    Xvnc has seen EOF, when it returns."""
    async def _run(home: str) -> set:
        sock_dir = os.path.join(home, "bot-desktop")
        os.makedirs(sock_dir, exist_ok=True)
        xvnc_saw_eof = asyncio.Event()

        async def _xvnc(reader, writer):
            await reader.read()  # until the bridge closes its end
            xvnc_saw_eof.set()

        server = await asyncio.start_unix_server(_xvnc, path=os.path.join(sock_dir, "rfb.sock"))
        try:
            await display._bridge(_Ws(1000), {"hermes_home": home, "viewer_id": "desk-1"})
            leftover = {t for t in asyncio.all_tasks()
                        if not t.done() and t.get_coro().__qualname__.startswith("_bridge.")}
            await asyncio.wait_for(xvnc_saw_eof.wait(), 2)
            return leftover
        finally:
            server.close()

    lease._reset_for_tests()
    with tempfile.TemporaryDirectory() as home:
        leftover = asyncio.run(_run(home))
    assert leftover == set(), [t.get_coro() for t in leftover]
