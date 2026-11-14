"""Actual RFB bridge with synthetic pipes and a deterministic clock; no browser or network."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli.web_routers import display
from tools.bot_desktop import lease


class Ws:
    def __init__(self, code):
        self.code = code
        self.finish = asyncio.Event()
        self.closed = False

    async def receive(self):
        await self.finish.wait()
        return {"type": "websocket.disconnect", "code": self.code}

    async def send_bytes(self, data):
        pass

    async def close(self, code=1000, reason=""):
        self.closed = True


class Writer:
    def __init__(self, gate=None):
        self.gate = gate
        self.closing = asyncio.Event()

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closing.set()

    async def wait_closed(self):
        if self.gate is not None:
            await self.gate.wait()


class Clock:
    """Explicit clock turns instead of timing-sensitive wall-clock sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.waiters = []
        self.yield_once = asyncio.sleep

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        await future

    async def flush(self):
        for _ in range(12):
            await self.yield_once(0)

    async def advance(self, seconds):
        self.now += seconds
        due = [future for at, future in self.waiters if at <= self.now]
        self.waiters = [(at, future) for at, future in self.waiters if at > self.now]
        for future in due:
            if not future.done():
                future.set_result(None)
        await self.flush()


class Bridge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = self.temp.name
        self.pipes = {}
        self.open_patch = patch.object(display, "_open_rfb", self._pipes)
        self.open_patch.start()

    def tearDown(self):
        self.open_patch.stop()
        self.temp.cleanup()

    def raw(self):
        return json.loads((Path(self.home) / "bot-desktop/lease.json").read_text())

    async def _pipes(self, _):
        reader, writer, error, gate = self.pipes[asyncio.current_task()]
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        return reader, writer, None

    async def open(self, ws, writer=None, *, error=None, gate=None):
        reader = asyncio.StreamReader()
        writer = writer or Writer()
        task = asyncio.current_task()
        self.pipes[task] = reader, writer, error, gate
        try:
            await display._bridge(
                ws, {"hermes_home": self.home, "viewer_id": "fixture-viewer"}
            )
        finally:
            self.pipes.pop(task)

    def run_clock(self, run):
        clock = Clock()
        with (
            patch.object(lease.time, "time", lambda: clock.now),
            patch.object(display.asyncio, "sleep", clock.sleep),
        ):
            asyncio.run(run(clock))

    def test_abnormal_close_persists_handback_without_any_later_lease_read(self):
        async def run(clock):
            lease.acquire("fixture-viewer", profile_key=self.home)
            ws = Ws(1012)
            ws.finish.set()
            await self.open(ws)
            await clock.flush()
            await clock.advance(31)
            self.assertEqual(self.raw()["holder"], "agent")

        self.run_clock(run)

    def test_active_bridge_renews_beyond_deadline_then_drop_expires(self):
        async def run(clock):
            lease.acquire("fixture-viewer", profile_key=self.home)
            ws = Ws(1012)
            task = asyncio.create_task(self.open(ws))
            await clock.flush()
            for _ in range(6):
                await clock.advance(10)
            self.assertEqual(self.raw()["holder"], "human")
            self.assertGreater(self.raw()["expires_at"], clock.now)
            ws.finish.set()
            await task
            await clock.flush()
            await clock.advance(31)
            self.assertEqual(self.raw()["holder"], "agent")

        self.run_clock(run)

    def test_idle_viewer_releases_control_despite_framebuffer_requests(self):
        async def run(clock):
            asyncio.get_running_loop().time = lambda: clock.now
            lease.acquire("fixture-viewer", profile_key=self.home)

            class Viewer(Ws):
                def __init__(self):
                    super().__init__(1000)
                    self.messages = asyncio.Queue()

                async def receive(self):
                    return {"type": "websocket.receive", "bytes": await self.messages.get()}

                async def close(self, code=1000, reason=""):
                    if reason:
                        self.disconnect_reason = reason

            ws = Viewer()
            task = asyncio.create_task(self.open(ws))
            await clock.flush()
            ws.messages.put_nowait(b"RFB 003.008\n\x01\x01")
            await clock.advance(8)
            ws.messages.put_nowait(bytes([4]) + bytes(7))
            await clock.flush()
            await clock.advance(2)
            self.assertFalse(task.done())
            await clock.advance(7)
            ws.messages.put_nowait(bytes([3]) + bytes(9))
            await clock.flush()
            await clock.advance(1)
            await task
            self.assertEqual(ws.disconnect_reason, "desktop-idle")
            self.assertEqual(self.raw()["holder"], "agent")

        with patch.object(display, "_desktop_viewer_idle_seconds", return_value=10):
            self.run_clock(run)

    def test_deferred_clean_close_cannot_release_newer_same_viewer_epoch(self):
        async def run(clock):
            old = lease.acquire("fixture-viewer", profile_key=self.home)
            gate = asyncio.Event()
            writer = Writer(gate)
            ws = Ws(1000)
            task = asyncio.create_task(self.open(ws, writer))
            await clock.flush()
            ws.finish.set()
            await writer.closing.wait()
            lease.release(
                "fixture-viewer", expected_epoch=old.epoch, profile_key=self.home
            )
            new = lease.acquire("fixture-viewer", profile_key=self.home)
            gate.set()
            await task
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], new.epoch)

        self.run_clock(run)

    def test_old_expiry_task_cannot_revoke_new_viewer(self):
        async def run(clock):
            lease.acquire("fixture-viewer", profile_key=self.home)
            ws = Ws(1012)
            ws.finish.set()
            await self.open(ws)
            await clock.flush()
            await clock.advance(10)
            other = lease.acquire("another-viewer", profile_key=self.home)
            await clock.advance(21)
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], other.epoch)

        self.run_clock(run)

    def test_reconnect_within_grace_keeps_lease_then_final_drop_expires(self):
        async def run(clock):
            old = lease.acquire("fixture-viewer", profile_key=self.home)
            first = Ws(1012)
            first.finish.set()
            await self.open(first)
            await clock.flush()
            await clock.advance(10)
            second = Ws(1012)
            task = asyncio.create_task(self.open(second))
            await clock.flush()
            for _ in range(4):
                await clock.advance(10)
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], old.epoch)
            second.finish.set()
            await task
            await clock.flush()
            await clock.advance(31)
            self.assertEqual(self.raw()["holder"], "agent")

        self.run_clock(run)

    def test_input_gate_rechecks_expiry_before_forwarding_frame(self):
        from tools.bot_desktop import rfb_filter

        captured = {}

        class Filter(rfb_filter.RfbClientFilter):
            def __init__(self, allow, **kwargs):
                super().__init__(allow, **kwargs)
                captured["allow"] = allow

        async def run(clock):
            lease.acquire("fixture-viewer", profile_key=self.home)
            ws = Ws(1000)
            task = asyncio.create_task(self.open(ws))
            await clock.flush()
            self.assertTrue(captured["allow"]())
            current = lease.get(self.home)
            current.expires_at = clock.now - 1
            lease._write(Path(self.home) / "bot-desktop/lease.json", current)
            self.assertFalse(captured["allow"]())
            self.assertEqual(self.raw()["holder"], "agent")
            ws.finish.set()
            await task

        with patch.object(rfb_filter, "RfbClientFilter", Filter):
            self.run_clock(run)

    def test_reacquire_and_replacement_before_old_close_preserves_new_stream(self):
        async def run(clock):
            old = lease.acquire("fixture-viewer", profile_key=self.home)
            first = Ws(1000)
            first_task = asyncio.create_task(self.open(first))
            await clock.flush()
            lease.release(
                "fixture-viewer", expected_epoch=old.epoch, profile_key=self.home
            )
            current = lease.acquire("fixture-viewer", profile_key=self.home)
            # The old bridge has already observed the NEW epoch before replacement opens.
            await clock.flush()
            second = Ws(1012)
            second_task = asyncio.create_task(self.open(second))
            await clock.flush()
            first.finish.set()
            await first_task
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], current.epoch)
            for _ in range(4):
                await clock.advance(10)
            self.assertEqual(self.raw()["holder"], "human")
            second.finish.set()
            await second_task
            await clock.flush()
            await clock.advance(31)
            self.assertEqual(self.raw()["holder"], "agent")

        self.run_clock(run)

    def test_same_existing_stream_can_hand_back_and_reacquire_without_replacement(self):
        async def run(clock):
            old = lease.acquire("fixture-viewer", profile_key=self.home)
            ws = Ws(1000)
            task = asyncio.create_task(self.open(ws))
            await clock.flush()
            lease.release(
                "fixture-viewer", expected_epoch=old.epoch, profile_key=self.home
            )
            current = lease.acquire("fixture-viewer", profile_key=self.home)
            await clock.flush()
            for _ in range(4):
                await clock.advance(10)
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], current.epoch)
            ws.finish.set()
            await task
            self.assertEqual(self.raw()["holder"], "agent")

        self.run_clock(run)

    def test_failed_replacement_open_preserves_healthy_stream_and_cleans_attempt(self):
        async def run(clock):
            current = lease.acquire("fixture-viewer", profile_key=self.home)
            first = Ws(1000)
            first_task = asyncio.create_task(self.open(first))
            await clock.flush()
            await self.open(Ws(1000), error=OSError("synthetic-open-failure"))
            for _ in range(4):
                await clock.advance(10)
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], current.epoch)
            first.finish.set()
            await first_task
            self.assertFalse(
                any(
                    key[0] == str(Path(self.home).resolve())
                    for key in display._viewer_streams
                )
            )

        self.run_clock(run)

    def test_older_pending_open_cannot_reclaim_after_newer_stream_closed(self):
        async def run(clock):
            lease.acquire("fixture-viewer", profile_key=self.home)
            gate = asyncio.Event()
            pending = Ws(1000)
            pending_task = asyncio.create_task(self.open(pending, gate=gate))
            await clock.flush()
            newer = Ws(1000)
            newer_task = asyncio.create_task(self.open(newer))
            await clock.flush()
            newer.finish.set()
            await newer_task
            current = lease.acquire("fixture-viewer", profile_key=self.home)
            gate.set()
            await pending_task
            self.assertEqual(self.raw()["holder"], "human")
            self.assertEqual(self.raw()["epoch"], current.epoch)
            self.assertFalse(
                any(
                    key[0] == str(Path(self.home).resolve())
                    for key in display._viewer_streams
                )
            )

        self.run_clock(run)


if __name__ == "__main__":
    unittest.main()
