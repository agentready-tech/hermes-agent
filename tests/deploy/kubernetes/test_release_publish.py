"""Private publication authority and the immutable Hermes source identity stay separate."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy.kubernetes import publish_release as publish


class PublicationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'GITHUB_REPOSITORY': publish.PUBLISHER_REPOSITORY})
        environment.start()
        self.addCleanup(environment.stop)

    def test_visibility_never_substitutes_for_repository_ownership(self):
        for visibility in ("public", "private", "internal"):
            value = {"id": 16663531, "name": "agentsee-hermes-runtime", "owner": {"login": "agentready-tech"},
                "visibility": visibility, "repository": {"full_name": publish.PUBLISHER_REPOSITORY}}
            if visibility == "private":
                self.assertEqual(publish.registration(value)["packageVisibility"], visibility)
            else:
                with self.assertRaises(ValueError):
                    publish.registration(value)
            for repository in ({"full_name": publish.SOURCE_REPOSITORY}, {"full_name": "another/hermes-agent"}):
                with self.assertRaises(ValueError):
                    publish.registration({**value, "repository": repository})
        with self.assertRaises(ValueError):
            publish.registration({"repository": {"full_name": "agentready-tech/hermes-agent"}})
        observed = {"id": 16663531, "name": "agentsee-hermes-runtime", "owner": {"login": "agentready-tech"},
                    "visibility": "private", "repository": None}
        self.assertFalse(publish.registration(observed)['packageRepositoryAssociationAvailable'])

    def test_published_manifest_uses_the_same_package_before_and_after_push(self):
        package = {"id": 16663531, "name": "agentsee-hermes-runtime", "owner": {"login": "agentready-tech"},
            "visibility": "private", "repository": {"full_name": publish.PUBLISHER_REPOSITORY}}
        path = "orgs/agentready-tech/packages/container/agentsee-hermes-runtime"
        digest = "sha256:" + "a" * 64
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if args[:2] == ["gh", "api"]:
                if args[2] == "repos/" + publish.PUBLISHER_REPOSITORY:
                    return json.dumps({"private": True}).encode()
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
        self.assertEqual(facts["packageVisibility"], "private")
        self.assertEqual(facts["sourceRepository"], publish.SOURCE_REPOSITORY)
        self.assertEqual(facts["packagePublisherRepository"], publish.PUBLISHER_REPOSITORY)
        self.assertIn(["gh", "api", path], calls)

    def test_registry_failure_is_checked_before_source_export_and_build(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(publish, 'output_root', return_value=Path(directory) / 'release'), \
                patch.object(publish, 'phase'), patch.object(publish, 'release_request', return_value=('a' * 40, '2026.11.7.6')), \
                patch.object(publish, 'registry_preflight', side_effect=ValueError('PACKAGE_ACCESS_REQUIRED')), \
                patch.object(publish, 'prepare') as prepare, patch.object(publish, 'build') as build:
            with self.assertRaisesRegex(ValueError, 'PACKAGE_ACCESS_REQUIRED'):
                publish.main()
            prepare.assert_not_called()
            build.assert_not_called()

    def test_missing_package_cannot_be_created_implicitly(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'GH_TOKEN': 'fixture-token', 'GITHUB_ACTOR': 'fixture'}), \
                patch.object(publish, 'output_root', return_value=Path(directory)), \
                patch.object(publish, 'run', return_value=b'{"private":true}') as command, \
                patch.object(publish.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'HTTP 404')):
            with self.assertRaisesRegex(ValueError, 'must already exist'):
                publish.registry_preflight(publish.IMAGE + ':2026.11.7.6')
            self.assertFalse(any(call.args[0][:2] == ['docker', 'push'] for call in command.call_args_list))

    def test_failed_build_keeps_only_a_bounded_redacted_private_diagnostic(self):
        def fail(args, **options):
            options['stdout'].write(b'x' * 23000 + b' fixture-workflow-token failed download')
            options['stdout'].flush()
            raise subprocess.CalledProcessError(1, args)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(publish, 'output_root', return_value=Path(directory)), \
                patch.dict(os.environ, {'GH_TOKEN': 'fixture-workflow-token'}), \
                patch.object(publish.subprocess, 'run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                publish.run(['docker', 'build', '.'], log='native-build.log')
            failure = Path(directory) / 'failure.private.log'
            self.assertLessEqual(len(failure.read_bytes()), 20000)
            self.assertNotIn(b'fixture-workflow-token', failure.read_bytes())
            self.assertIn(b'[REDACTED] failed download', failure.read_bytes())
            self.assertEqual(failure.stat().st_mode & 0o777, 0o600)


class RegistryPullTests(unittest.TestCase):
    def test_registry_roundtrip_verifies_all_blobs_without_an_image_archive(self):
        import hashlib
        candidate = publish.IMAGE + ':2026.11.7.3'
        digest = 'sha256:' + 'a' * 64
        image = publish.IMAGE + '@' + digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def pull(args, **_kwargs):
                destination = args[-1]
                self.assertEqual(destination, 'oci:' + str(root / 'oci') + ':' + candidate)
                layout = root / 'oci'; (layout / 'blobs/sha256').mkdir(parents=True)
                blob = b'fixture OCI content'
                (layout / 'blobs/sha256' / hashlib.sha256(blob).hexdigest()).write_bytes(blob)
                (layout / 'index.json').write_text(json.dumps({'manifests': [{'digest': digest,
                    'annotations': {'org.opencontainers.image.ref.name': candidate}}]}))
            with patch.object(publish, 'output_root', return_value=root), patch.object(publish, 'phase'), \
                    patch.object(publish, 'run', side_effect=pull):
                proof = publish.verify_registry_pull(image, candidate)
            self.assertTrue(proof['registryFullPullVerified'])
            self.assertFalse(proof['imageArchivePublished'])
            self.assertFalse(list(root.glob('*.tar')))
        with self.assertRaises(ValueError):
            publish.verify_registry_pull(image, 'release')
