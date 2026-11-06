"""Recovery keeps manifest identity and never retires another release."""
import json
import unittest
from unittest.mock import patch

from deploy.kubernetes import recover_release as recovery


class ReleaseRecoveryTests(unittest.TestCase):
    def request(self):
        return {'sourceRevision': 'a' * 40, 'version': '2026.11.6.1',
                'source': 'ghcr.io/agentready-tech/hermes-agent', 'destination': recovery.IMAGE,
                'digest': 'sha256:' + 'b' * 64, 'publicVersionId': 123}

    def test_destination_is_only_common_private_package(self):
        recovery.validate_request(self.request())
        for field, value in (('destination', recovery.IMAGE + '-owner'), ('sourceRevision', 'main'),
                             ('digest', 'latest'), ('version', 'latest')):
            with self.assertRaises(ValueError):
                recovery.validate_request({**self.request(), field: value})

    def test_public_or_unlinked_package_is_rejected(self):
        value = {'visibility': 'private', 'repository': {'full_name': 'agentready-tech/hermes-agent'}}
        self.assertTrue(recovery.private_package(value))
        self.assertFalse(recovery.private_package({**value, 'visibility': 'public'}))
        self.assertFalse(recovery.private_package({**value, 'repository': None}))

    def test_retirement_rejects_a_version_with_other_tags(self):
        value = {'name': self.request()['digest'], 'metadata': {'container': {'tags': ['2026.11.6.1', 'keep']}}}
        with patch.object(recovery, 'phase'), patch.object(recovery, 'run', return_value=json.dumps(value).encode()) as command:
            with self.assertRaisesRegex(ValueError, 'DELETE_SCOPE_CHANGED'):
                recovery.retire_public_attempt(self.request())
            self.assertEqual(command.call_count, 1)

    def test_retirement_rejects_different_manifest(self):
        value = {'name': 'sha256:' + 'c' * 64, 'metadata': {'container': {'tags': ['2026.11.6.1']}}}
        with patch.object(recovery, 'phase'), patch.object(recovery, 'run', return_value=json.dumps(value).encode()) as command:
            with self.assertRaisesRegex(ValueError, 'DELETE_SCOPE_CHANGED'):
                recovery.retire_public_attempt(self.request())
            self.assertEqual(command.call_count, 1)


if __name__ == '__main__':
    unittest.main()
