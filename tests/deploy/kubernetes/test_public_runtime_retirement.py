"""Deletion is confined to five unchanged public manifests, after pinned private proofs."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.kubernetes import retire_public_runtime as cleanup

ROOT = Path(__file__).resolve().parents[3]


def evidence():
    _, expected, _ = cleanup.load_evidence(ROOT)
    return expected


def package():
    return {'id': 16640069, 'name': 'agentsee-hermes', 'visibility': 'public', 'package_type': 'container',
            'owner': {'login': 'agentready-tech'}, 'version_count': 5,
            'repository': {'full_name': cleanup.REPOSITORY}}


def versions():
    return [{'id': row['id'], 'name': row['digest'], 'metadata': {'container': {'tags': row['tags']}}}
            for row in evidence()]


class PublicRetirementGuards(unittest.TestCase):
    def test_real_pinned_private_copies_and_completed_rollout_validate(self):
        request, expected, fingerprint = cleanup.load_evidence(ROOT)
        self.assertEqual({row['id'] for row in expected}, cleanup.VERSION_IDS)
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(request['privatePackage']['id'], 16663531)
        self.assertEqual(request['allowedPrivateExtraVersions'][0]['version'], '2026.11.7.6')

    def test_changed_package_id_name_owner_visibility_or_binding_refused(self):
        cleanup.validate_public(package(), versions(), evidence())
        for changes in [{'id': 16663531}, {'name': 'agentsee-hermes-runtime'}, {'visibility': 'private'},
                        {'owner': {'login': 'foreign'}}, {'repository': {'full_name': 'foreign/repo'}},
                        {'version_count': 6}, {'package_type': 'npm'}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'PACKAGE_SCOPE_CHANGED'):
                cleanup.validate_public({**package(), **changes}, versions(), evidence())

    def test_changed_version_ids_digest_or_tags_refused(self):
        for key, value in [('id', 1), ('name', 'sha256:' + '0'*64),
                           ('metadata', {'container': {'tags': ['2026.11.7.5', 'new-tag']}})]:
            changed = versions(); changed[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                cleanup.validate_public(package(), changed, evidence())
        with self.assertRaisesRegex(ValueError, 'VERSION_SET_CHANGED'):
            cleanup.validate_public(package(), versions()[:-1], evidence())

    def test_changed_private_package_or_manifest_does_not_authorize_delete(self):
        copies = json.loads((ROOT / cleanup.PROOFS['privateCopies'][0]).read_text())
        rollout = json.loads((ROOT / cleanup.PROOFS['completedRollout'][0]).read_text())
        for change in [{'packageId': 16663044}, {'private': False}, {'bootstrapMarkerRetired': False},
                       {'historicalCopiesPending': True}, {'sourceRepository': cleanup.PUBLISHER}]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'PRIVATE_ROLLBACK_PUBLICATION'):
                cleanup.validate_evidence(evidence(), {**copies, **change}, rollout)
        changed = copy.deepcopy(copies); changed['versions'][0]['digest'] = 'sha256:' + '0'*64
        with self.assertRaisesRegex(ValueError, 'PRIVATE_ROLLBACK_IDENTITY'):
            cleanup.validate_evidence(evidence(), changed, rollout)

    def test_incomplete_all4_or_metadata_push_refused(self):
        copies = json.loads((ROOT / cleanup.PROOFS['privateCopies'][0]).read_text())
        rollout = json.loads((ROOT / cleanup.PROOFS['completedRollout'][0]).read_text())
        for change in [{'targets': cleanup.TARGETS[:-1]}, {'futureDefaultsVerified': False}, {'phase': 'pending'}]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'PRIVATE_COMPLETION'):
                cleanup.validate_evidence(evidence(), copies, {**rollout, **change})
        changed = copy.deepcopy(rollout); changed['metadata']['hermes']['pushed'] = False
        with self.assertRaisesRegex(ValueError, 'BOTH_METADATA_PUSHES'):
            cleanup.validate_evidence(evidence(), copies, changed)

    def test_pinned_proof_content_tampering_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in ['deploy/kubernetes/retire-public-runtime-request.json',
                         'deploy/kubernetes/runtime-private-request.json', *(row[0] for row in cleanup.PROOFS.values())]:
                target = root / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / path).read_bytes())
            target = root / cleanup.PROOFS['privateCopies'][0]
            target.write_bytes(target.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'ROOT_PROOF_HASH_MISMATCH'):
                cleanup.load_evidence(root)

    def test_initial_404_is_uncertain_and_never_deletes_again(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cleanup, 'api', return_value=None) as api:
            with self.assertRaisesRegex(ValueError, '404_BEFORE_DELETE_IS_UNCERTAIN'):
                cleanup.retire(evidence(), {}, Path(temporary) / 'proof.json')
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args, (cleanup.RESOURCE,))

    def test_exact_public_delete_ack_then_404_is_recorded(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(cleanup, 'api', side_effect=[package(), versions(), None, None]) as api:
            result = cleanup.retire(evidence(), {}, Path(temporary) / 'proof.json')
            self.assertEqual(result['phase'], 'complete')
            self.assertTrue(result['formerPublicPackageApi404'])
            self.assertTrue(result['privatePackageUntouched'])
        writes = [call for call in api.call_args_list if call.kwargs.get('method') == 'DELETE']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args, (cleanup.RESOURCE,))

    def test_delete_error_and_unconfirmed_post_delete_never_report_complete(self):
        for final in [RuntimeError('PUBLIC_PACKAGE_DELETE_FAILED'), package()]:
            responses = [package(), versions(), final] if isinstance(final, Exception) else [package(), versions(), None, final]
            with tempfile.TemporaryDirectory() as temporary, patch.object(cleanup, 'api', side_effect=responses):
                output = Path(temporary) / 'proof.json'
                with self.assertRaises((ValueError, RuntimeError)):
                    cleanup.retire(evidence(), {}, output)
                self.assertNotEqual(json.loads(output.read_text())['phase'], 'complete')

    def test_api_cannot_target_private_or_old_failed_destination(self):
        with patch.object(cleanup.subprocess, 'run') as run:
            for path in ['orgs/agentready-tech/packages/container/agentsee-hermes-runtime',
                         cleanup.RESOURCE + '/versions/1520055394']:
                with self.assertRaisesRegex(ValueError, 'PUBLIC_API_SCOPE_REQUIRED'):
                    cleanup.api(path, method='DELETE')
        run.assert_not_called()

    def test_non404_transport_or_authority_error_is_not_absence(self):
        for error in [b'HTTP 403 Forbidden', b'HTTP 500', b'connection timeout']:
            with patch.object(cleanup.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr=error)), \
                    self.assertRaisesRegex(RuntimeError, 'GET_FAILED'):
                cleanup.api(cleanup.RESOURCE)

    def test_workflow_has_only_manual_scoped_metadata_job(self):
        workflow = (ROOT / '.github/workflows/agentsee-retire-public-runtime.yml').read_text()
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('packages: write', workflow)
        for forbidden in ('docker build', 'skopeo', 'workflow_run:', 'tags:', 'secrets.'):
            self.assertNotIn(forbidden, workflow)


if __name__ == '__main__':
    unittest.main()
