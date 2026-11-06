"""Publication records existing visibility honestly and always checks our package."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy.kubernetes import publish_release as publish


class PublicationTests(unittest.TestCase):
    def test_visibility_never_substitutes_for_repository_ownership(self):
        for visibility in ("public", "private", "internal"):
            value = {"visibility": visibility, "repository": {"full_name": "agentready-tech/hermes-agent"}}
            self.assertEqual(publish.registration(value)["packageVisibility"], visibility)
            for repository in (None, {"full_name": "another/hermes-agent"}):
                with self.assertRaises(ValueError):
                    publish.registration({**value, "repository": repository})
        with self.assertRaises(ValueError):
            publish.registration({"repository": {"full_name": "agentready-tech/hermes-agent"}})

    def test_published_manifest_uses_the_same_package_before_and_after_push(self):
        package = {"visibility": "public", "repository": {"full_name": "agentready-tech/hermes-agent"}}
        path = "orgs/agentready-tech/packages/container/agentsee-hermes"
        digest = "sha256:" + "a" * 64
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if args[:2] == ["gh", "api"]:
                self.assertEqual(args[2], path)
                return json.dumps(package).encode()
            if args[:2] == ["skopeo", "inspect"]:
                return digest.encode()
            return b""

        def lookup(args, **kwargs):
            if args[0] == "gh":
                self.assertEqual(args, ["gh", "api", path])
                return subprocess.CompletedProcess(args, 0, json.dumps(package).encode(), b"")
            return subprocess.CompletedProcess(args, 1, b"", b"manifest unknown")

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GH_TOKEN": "fixture-token", "GITHUB_ACTOR": "fixture"}), \
                patch.object(publish, "output_root", return_value=Path(directory)), \
                patch.object(publish, "phase"), patch.object(publish, "run", side_effect=command), \
                patch.object(publish.subprocess, "run", side_effect=lookup):
            image, facts = publish.publish(publish.IMAGE + ":2026.11.7.1")
        self.assertEqual(image, publish.IMAGE + "@" + digest)
        self.assertEqual(facts["packageVisibility"], "public")
        self.assertIn(["gh", "api", path], calls)
