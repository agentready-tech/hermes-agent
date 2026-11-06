"""Release packaging rejects dirty exports and inherited application modules."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy.kubernetes.release_source import MANIFEST, PLUGIN, PLUGIN_MANIFEST, PLUGIN_REVISION, export, verify


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "agent").mkdir()
        (self.root / "tools").mkdir()
        self.module = self.root / "agent/tool_continuation.py"
        self.module.write_text('"""Committed continuation implementation."""\n')
        self.revision = "a" * 40
        (self.root / "tools/facts.json").write_text("{}")
        manifest = {"sourceRevision": self.revision, "moduleRoots": ["agent", "tools", "plugins"],
                    "files": {"agent/tool_continuation.py": hashlib.sha256(self.module.read_bytes()).hexdigest()}}
        (self.root / MANIFEST).write_text(json.dumps(manifest))
        (self.root / "install-stamp.json").write_text(json.dumps({"commit": self.revision, "dirty": False}))

    def test_exact_committed_code_passes(self):
        self.assertTrue(verify(self.root)["cleanSourceVerified"])

    def test_inherited_upstream_module_fails(self):
        (self.root / "agent/sleep_clock.py").write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "Unversioned application"):
            verify(self.root)

    def test_missing_completed_fix_fails(self):
        self.module.unlink()
        with self.assertRaisesRegex(ValueError, "Expected regular source"):
            verify(self.root)

    def test_changed_code_fails(self):
        self.module.write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            verify(self.root)

    def test_stamp_must_match_source_commit(self):
        (self.root / "install-stamp.json").write_text(json.dumps({"commit": "b" * 40}))
        with self.assertRaisesRegex(ValueError, "same clean commit"):
            verify(self.root)

    def test_dependency_payload_requires_pm_record(self):
        tool = self.root / "tools/python-3.14.7-linux-x64/lib"
        tool.mkdir(parents=True)
        (tool / "stdlib.py").write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "Unrecorded managed dependency"):
            verify(self.root)
        (self.root / "tools/facts.json").write_text(json.dumps({"python": str(tool.parent)}))
        self.assertTrue(verify(self.root)["cleanSourceVerified"])

    def test_explicit_pinned_plugin_manifest(self):
        plugin = self.root / PLUGIN / "__init__.py"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "Unversioned application"):
            verify(self.root)
        value = {"revision": PLUGIN_REVISION, "files": {str(plugin.relative_to(self.root)):
                 hashlib.sha256(plugin.read_bytes()).hexdigest()}}
        (self.root / PLUGIN_MANIFEST).write_text(json.dumps(value))
        self.assertTrue(verify(self.root)["cleanSourceVerified"])

    def test_export_refuses_untracked_and_dirty_sources(self):
        repo = self.root / "checkout"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
        (repo / "main.py").write_text("pass\n")
        subprocess.run(["git", "-C", str(repo), "add", "main.py"], check=True, timeout=10)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Packaging test", "-c",
                        "user.email=packaging@example.invalid", "commit", "-qm", "Source"], check=True, timeout=10)
        revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=10).strip()
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/codex/agentready-runtime", revision], check=True, timeout=10)
        (repo / "unexpected.py").write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "dirty build checkout"):
            export(repo, self.root / "rejected", revision)
        (repo / "unexpected.py").unlink()
        self.assertIn("main.py", export(repo, self.root / "exported", revision)["files"])
        (repo / "main.py").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "dirty build checkout"):
            export(repo, self.root / "rejected-again", revision)


if __name__ == "__main__":
    unittest.main()
