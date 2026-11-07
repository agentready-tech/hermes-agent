"""Human-control liveness uses persisted state, not a forever exclusion."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.bot_desktop import lease


class Liveness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def now(self, value):
        return patch.object(lease.time, "time", return_value=value)

    def test_abandoned_human_control_expires_without_explicit_handback(self):
        with self.now(1000):
            lease.acquire("synthetic-viewer", profile_key=self.home)
        with self.now(1031):
            self.assertEqual(lease.get(self.home).holder, lease.AGENT)

    def test_legacy_human_control_without_deadline_does_not_live_forever(self):
        p = Path(self.home) / "bot-desktop/lease.json"
        p.parent.mkdir()
        p.write_text(
            json.dumps(
                {
                    "holder": "human",
                    "viewer_id": "synthetic-viewer",
                    "since": 1000,
                    "reason": "",
                    "epoch": 7,
                }
            )
        )
        with self.now(1031):
            current = lease.get(self.home)
        self.assertEqual(current.holder, lease.AGENT)
        self.assertEqual(current.epoch, 8)

    def test_live_viewer_renewal_preserves_control_beyond_original_deadline(self):
        with self.now(1000):
            current = lease.acquire("synthetic-viewer", profile_key=self.home)
        for t in (1010, 1020, 1030, 1040, 1050, 1060):
            with self.now(t):
                self.assertTrue(
                    lease.renew(
                        "synthetic-viewer",
                        expected_epoch=current.epoch,
                        profile_key=self.home,
                    )
                )
        with self.now(1089):
            current = lease.get(self.home)
        self.assertEqual(current.holder, lease.HUMAN)
        self.assertEqual(current.epoch, 1)

    def test_observation_does_not_extend_deadline_and_expiry_notifies_once(self):
        seen = []
        off = lease.on_change(lambda k, v: seen.append(v.holder))
        try:
            with self.now(1000):
                lease.acquire("synthetic-viewer", profile_key=self.home)
            for t in (1001, 1009, 1029):
                with self.now(t):
                    self.assertEqual(lease.get(self.home).holder, lease.HUMAN)
            with self.now(1030):
                self.assertEqual(lease.get(self.home).holder, lease.AGENT)
                self.assertEqual(lease.get(self.home).holder, lease.AGENT)
            self.assertEqual(seen, [lease.HUMAN, lease.AGENT])
        finally:
            off()

    def test_old_disconnect_cannot_release_same_viewer_new_epoch(self):
        with self.now(1000):
            old = lease.acquire("synthetic-viewer", profile_key=self.home)
            lease.release(
                "synthetic-viewer", expected_epoch=old.epoch, profile_key=self.home
            )
            new = lease.acquire("synthetic-viewer", profile_key=self.home)
            current = lease.release(
                "synthetic-viewer", expected_epoch=old.epoch, profile_key=self.home
            )
        self.assertEqual(current.holder, lease.HUMAN)
        self.assertEqual(current.epoch, new.epoch)

    def test_expired_old_viewer_cannot_renew_or_send_input(self):
        with self.now(1000):
            current = lease.acquire("synthetic-viewer", profile_key=self.home)
        with self.now(1031):
            self.assertFalse(
                lease.renew(
                    "synthetic-viewer",
                    expected_epoch=current.epoch,
                    profile_key=self.home,
                )
            )
            self.assertFalse(
                lease.viewer_may_send_input("synthetic-viewer", profile_key=self.home)
            )
            self.assertEqual(lease.assert_agent_may_act(self.home).holder, lease.AGENT)

    def test_foreign_viewer_and_old_epoch_cannot_renew_or_release(self):
        with self.now(1000):
            old = lease.acquire("viewer-old", profile_key=self.home)
            new = lease.acquire("viewer-new", profile_key=self.home)
        with self.now(1020):
            self.assertFalse(
                lease.renew(
                    "viewer-old", expected_epoch=old.epoch, profile_key=self.home
                )
            )
            self.assertEqual(
                lease.release(
                    "viewer-old", expected_epoch=old.epoch, profile_key=self.home
                ).epoch,
                new.epoch,
            )
        with self.now(1031):
            self.assertEqual(lease.get(self.home).holder, lease.AGENT)

    def test_restart_reads_persisted_deadline_without_process_registry(self):
        with self.now(1000):
            current = lease.acquire("synthetic-viewer", profile_key=self.home)
        with self.now(1020):
            self.assertTrue(
                lease.renew(
                    "synthetic-viewer",
                    expected_epoch=current.epoch,
                    profile_key=self.home,
                )
            )
        # Re-importing the actual module models another process reading the persisted state.
        fresh_spec = importlib.util.spec_from_file_location(
            "lease_review_restarted", Path(lease.__file__)
        )
        fresh = importlib.util.module_from_spec(fresh_spec)
        sys.modules[fresh_spec.name] = fresh
        fresh_spec.loader.exec_module(fresh)
        with patch.object(fresh.time, "time", return_value=1051):
            self.assertEqual(fresh.get(self.home).holder, fresh.AGENT)

    def test_corrupt_or_unreadable_lease_remains_fail_closed(self):
        p = Path(self.home) / "bot-desktop/lease.json"
        p.parent.mkdir()
        p.write_text("{invalid")
        with self.now(10**9):
            self.assertEqual(lease.get(self.home).holder, lease.HUMAN)
        self.assertEqual(p.read_text(), "{invalid")

    def test_explicit_acquire_after_expiry_creates_fresh_same_viewer_epoch(self):
        with self.now(1000):
            old = lease.acquire("synthetic-viewer", profile_key=self.home)
        with self.now(1031):
            new = lease.acquire("synthetic-viewer", profile_key=self.home)
            self.assertEqual(new.holder, lease.HUMAN)
            self.assertEqual(new.epoch, old.epoch + 1)
            self.assertEqual(new.expires_at, 1061)

    def test_malformed_deadline_or_epoch_never_expires_fail_closed_state(self):
        p = Path(self.home) / "bot-desktop/lease.json"
        p.parent.mkdir()
        for extra in (
            {"expires_at": "past"},
            {"since": "past"},
            {"epoch": "7"},
            {"expires_at": True},
        ):
            data = {
                "holder": "human",
                "viewer_id": "synthetic-viewer",
                "since": 1000,
                "epoch": 7,
                **extra,
            }
            raw = json.dumps(data)
            p.write_text(raw)
            with self.now(10**9):
                self.assertEqual(lease.get(self.home).holder, lease.HUMAN)
            self.assertEqual(p.read_text(), raw)

    def test_public_shape_never_exposes_private_deadline_or_viewer_capability(self):
        with self.now(1000):
            current = lease.acquire("synthetic-viewer", profile_key=self.home)
        public = lease.public_view(current)
        self.assertNotIn("expires_at", public)
        self.assertIsNone(public["viewer_id"])
        self.assertNotIn("synthetic-viewer", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
