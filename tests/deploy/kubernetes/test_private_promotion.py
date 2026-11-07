"""Private registry copy preserves immutable releases and distinguishes absence from denied access."""
import copy
import json
import subprocess
from unittest.mock import patch

import pytest

from deploy.kubernetes import promote_private_runtime as promote


def row():
    return {'version': '2026.11.7.5', 'tags': ['2026.11.7.5'],
            'digest': 'sha256:' + 'a' * 64, 'sourceRevision': 'b' * 40}


def request():
    return {'source': promote.SOURCE, 'destination': promote.DESTINATION,
            'versions': [row()], 'activeVersion': '2026.11.7.5'}


def test_request_cannot_target_employee_package_or_duplicate_release():
    promote.validate(request())
    for changed in ({'destination': promote.DESTINATION + '-owner'}, {'activeVersion': 'unknown'},
                    {'versions': [row(), row()]}):
        with pytest.raises(ValueError):
            promote.validate({**request(), **changed})


def test_proven_package_absence_does_not_probe_uncreated_registry_repository():
    with patch.object(promote, 'package_metadata', return_value=None), \
            patch.object(promote.subprocess, 'run') as registry:
        assert promote.target_exists(row()) is False
    registry.assert_not_called()


def test_existing_private_repository_denial_is_not_absence():
    result = subprocess.CompletedProcess([], 1, b'', b'403 denied')
    with patch.object(promote, 'package_metadata', return_value={'visibility': 'private'}), \
            patch.object(promote.subprocess, 'run', return_value=result):
        with pytest.raises(ValueError, match='LOOKUP_UNCERTAIN'):
            promote.target_exists(row())


def test_api403_is_never_treated_as_missing_package():
    result = subprocess.CompletedProcess([], 1, b'', b'HTTP 403: denied')
    with patch.object(promote.subprocess, 'run', return_value=result), \
            pytest.raises(RuntimeError, match='METADATA_UNAVAILABLE'):
        promote.package_metadata()


def test_private_repository_tag_conflict_is_never_overwritten():
    result = subprocess.CompletedProcess([], 0, json.dumps({'Digest': 'sha256:' + 'c' * 64}).encode(), b'')
    with patch.object(promote, 'package_metadata', return_value={'visibility': 'private'}), \
            patch.object(promote.subprocess, 'run', return_value=result):
        with pytest.raises(ValueError, match='TAG_CONFLICT'):
            promote.target_exists(row())


def test_existing_matching_manifest_skips_copy_and_public_package_is_refused():
    value = row()
    image = promote.DESTINATION + '@' + value['digest']
    with patch.object(promote, 'source_identity', return_value=promote.SOURCE + '@' + value['digest']), \
            patch.object(promote, 'target_exists', return_value=True), \
            patch.object(promote, 'package_metadata', return_value={'visibility': 'private'}), \
            patch.object(promote, 'run', return_value=json.dumps({'Digest': value['digest']}).encode()), \
            patch.object(promote.subprocess, 'run') as copy_command:
        assert promote.copy_version(value)['privateImage'] == image
    copy_command.assert_not_called()
    meta = {'visibility': 'public', 'repository': {'full_name': promote.REPOSITORY}}
    result = subprocess.CompletedProcess([], 0, json.dumps(meta).encode(), b'')
    with patch.object(promote.subprocess, 'run', return_value=result), \
            pytest.raises(ValueError, match='PRIVATE_FORK_PACKAGE_REQUIRED'):
        promote.package_metadata()


def test_source_revision_version_repository_and_digest_are_verified_before_copy():
    value = row()
    labels = {'org.opencontainers.image.revision': value['sourceRevision'],
              'org.opencontainers.image.version': value['version'],
              'org.opencontainers.image.source': 'https://github.com/' + promote.REPOSITORY}
    info = {'Digest': value['digest'], 'Labels': labels}
    with patch.object(promote, 'run', return_value=json.dumps(info).encode()):
        assert promote.source_identity(value) == promote.SOURCE + '@' + value['digest']
    for key in labels:
        changed = copy.deepcopy(info); changed['Labels'][key] = 'foreign'
        with patch.object(promote, 'run', return_value=json.dumps(changed).encode()), \
                pytest.raises(ValueError, match='LABELS_MISMATCH'):
            promote.source_identity(value)
