"""Retire exactly the former public runtime package after reviewed private rollback proofs.

The fork token reads/deletes ONLY its former public package. Private-package
evidence is pinned root-verified CI evidence; the dispatch records root's fresh
private registry metadata guard, without injecting a cross-repository token.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY = 'agentready-tech/hermes-agent'
PUBLISHER = 'agentready-tech/AgentReady'
RESOURCE = 'orgs/agentready-tech/packages/container/agentsee-hermes'
PRIVATE = 'ghcr.io/agentready-tech/agentsee-hermes-runtime'
PUBLIC_ID = 16640069
PRIVATE_ID = 16663531
VERSION_IDS = {1520055394, 1519827798, 1518198311, 1518120737, 1516253479}
TARGETS = ['agentsee-a-0dbfa61addbb40a88d636e18e7172117',
           'agentsee-a-87f2912883de429cb87db22e08345d01',
           'agentsee-a-e29bea46f65648fbb5bfb1c413d2854d', 'agentsee-mike']
PROOFS = {
    'privateCopies': ('deploy/kubernetes/public-retirement/private-copy-proof.json',
                      '6ec213d32442bd0890e75adb94bca84c63c44d1b02e0a6ea0b36855ae99affed'),
    'completedRollout': ('deploy/kubernetes/public-retirement/watchdog-completion-proof.json',
                         'd5e0e7d8be0e3d52ccd4235256fb90efe1afe38d862661a96a7f239981545747')}


def require(value, code):
    if not value:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def api(path, method='GET'):
    require(path in (RESOURCE, RESOURCE + '/versions?per_page=100')
            and (method == 'GET' or method == 'DELETE' and path == RESOURCE), 'PUBLIC_API_SCOPE_REQUIRED')
    result = subprocess.run(['gh', 'api', '--method', method, path], capture_output=True, timeout=60, check=False)
    if result.returncode:
        if method == 'GET' and b'HTTP 404' in result.stderr:
            return None
        raise RuntimeError('PUBLIC_PACKAGE_' + method + '_FAILED')
    return json.loads(result.stdout) if result.stdout.strip() else None


def load_evidence(root):
    request_raw = (root / 'deploy/kubernetes/retire-public-runtime-request.json').read_bytes()
    request = json.loads(request_raw)
    original_path = 'deploy/kubernetes/runtime-private-request.json'
    original_raw = (root / original_path).read_bytes()
    require(request.get('sourceVersionsRequest') == original_path
            and request.get('sourceVersionsRequestSha256') == sha(original_raw), 'ORIGINAL_VERSION_REQUEST_CHANGED')
    expected = json.loads(original_raw)['versions']
    require({row['id'] for row in expected} == VERSION_IDS and len(expected) == 5, 'FIVE_ORIGINAL_RELEASES_REQUIRED')
    require(request.get('publicPackage') == {'id': PUBLIC_ID, 'name': 'agentsee-hermes',
            'owner': 'agentready-tech', 'visibility': 'public', 'repository': REPOSITORY}, 'PUBLIC_REQUEST_SCOPE_CHANGED')
    require(request.get('privatePackage') == {'id': PRIVATE_ID, 'name': 'agentsee-hermes-runtime',
            'owner': 'agentready-tech', 'visibility': 'private', 'packagePublisherRepository': PUBLISHER},
            'PRIVATE_REQUEST_SCOPE_CHANGED')
    require(request.get('allowedPrivateExtraVersions') == [{'version': '2026.11.7.6',
            'sourceRevision': '7d0f4bb8448b4422bba6ad7bb589b68a5e037e26',
            'digest': 'sha256:87aa93589e0ced4c37dfd88cb51a9c1dd9b672501619fd91b1f78e4c23a47dd5'}]
            and request.get('deleteOnlyFormerPublicPackage') is True and request.get('productionMutation') is False,
            'PRIVATE_EXTRA_VERSION_OR_RETIREMENT_SCOPE_CHANGED')
    proofs = {}
    for name, (path, expected_hash) in PROOFS.items():
        require(request.get('proofs', {}).get(name) == {'path': path, 'sha256': expected_hash},
                'ROOT_PROOF_REFERENCE_CHANGED')
        raw = (root / path).read_bytes()
        require(sha(raw) == expected_hash, 'ROOT_PROOF_HASH_MISMATCH')
        proofs[name] = json.loads(raw)
    validate_evidence(expected, proofs['privateCopies'], proofs['completedRollout'])
    return request, expected, sha(request_raw)


def validate_evidence(expected, copies, rollout):
    require(copies.get('phase') == 'complete' and copies.get('packageId') == PRIVATE_ID
            and copies.get('destination') == PRIVATE and copies.get('private') is True
            and copies.get('noRebuild') is True and copies.get('bootstrapMarkerRetired') is True
            and copies.get('historicalCopiesPending') is False and copies.get('activeRegistryFullPullVerified') is True
            and copies.get('packagePublisherRepository') == PUBLISHER and copies.get('sourceRepository') == REPOSITORY,
            'PRIVATE_ROLLBACK_PUBLICATION_REQUIRED')
    mirrors = {row['id']: row for row in copies.get('versions', [])}
    require(len(mirrors) == 5 and set(mirrors) == VERSION_IDS, 'FIVE_PRIVATE_ROLLBACKS_REQUIRED')
    for source in expected:
        row = mirrors[source['id']]
        require(all(row.get(key) == source[key] for key in ('digest', 'tags', 'version', 'sourceRevision'))
                and row.get('privateImage') == PRIVATE + '@' + source['digest'], 'PRIVATE_ROLLBACK_IDENTITY_CHANGED')
    require(rollout.get('phase') == 'complete' and rollout.get('targets') == TARGETS
            and rollout.get('futureDefaultsVerified') is True and rollout.get('guardInsideEverySandbox') is True
            and rollout.get('allActivePackagesPrivate') is True and rollout.get('rollbackVersionsPrivate') == 5
            and rollout.get('runtimeImage') == copies.get('activeImage')
            and rollout.get('runtimeSourceRevision') == copies.get('activeSourceRevision')
            and rollout.get('packagePublisherRepository') == PUBLISHER and rollout.get('sourceRepository') == REPOSITORY,
            'ALL4_AND_FUTURE_PRIVATE_COMPLETION_REQUIRED')
    for name, branch in [('agentready', 'main'), ('hermes', 'codex/agentready-runtime')]:
        value = rollout.get('metadata', {}).get(name, {})
        require(value.get('pushed') is True and value.get('branch') == branch
                and re.fullmatch('[a-f0-9]{40}', value.get('commit', '')), 'BOTH_METADATA_PUSHES_REQUIRED')


def validate_public(package, versions, expected):
    require(package is not None, 'PUBLIC_PACKAGE_404_BEFORE_DELETE_IS_UNCERTAIN')
    require(package.get('id') == PUBLIC_ID and package.get('name') == 'agentsee-hermes'
            and package.get('visibility') == 'public' and package.get('package_type') == 'container'
            and package.get('owner', {}).get('login') == 'agentready-tech'
            and (package.get('repository') or {}).get('full_name') == REPOSITORY
            and package.get('version_count') == 5, 'PUBLIC_PACKAGE_SCOPE_CHANGED')
    require(isinstance(versions, list) and len(versions) == 5, 'PUBLIC_VERSION_SET_CHANGED')
    actual = {row['id']: row for row in versions}
    require(len(actual) == 5 and set(actual) == VERSION_IDS, 'PUBLIC_VERSION_IDS_CHANGED')
    for row in expected:
        version = actual[row['id']]
        require(version.get('name') == row['digest']
                and version.get('metadata', {}).get('container', {}).get('tags') == row['tags'],
                'PUBLIC_VERSION_DIGEST_OR_TAG_CHANGED')


def save(path, proof):
    temporary = path.with_name(path.name + '.pending')
    with temporary.open('w') as stream:
        json.dump(proof, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def retire(expected, proof, output):
    package = api(RESOURCE)
    require(package is not None, 'PUBLIC_PACKAGE_404_BEFORE_DELETE_IS_UNCERTAIN')
    validate_public(package, api(RESOURCE + '/versions?per_page=100'), expected)
    proof.update(phase='preconditions_verified', publicPackageId=PUBLIC_ID,
                 publicVersions=[{key: row[key] for key in ('id', 'digest', 'tags')} for row in expected],
                 privatePackageId=PRIVATE_ID, privatePackageUntouched=True, deleteAttempted=True)
    save(output, proof)
    api(RESOURCE, method='DELETE')
    proof.update(phase='delete_acknowledged', deleteAcknowledged=True); save(output, proof)
    require(api(RESOURCE) is None, 'PUBLIC_PACKAGE_DELETE_NOT_CONFIRMED')
    proof.update(phase='complete', formerPublicPackageApi404=True, onlyFormerPublicPackageDeleted=True,
                 privateRollbackVersionsPreservedByPinnedProof=5, productionChanged=False,
                 imagesBuiltOrDownloaded=False, tokensChanged=False)
    save(output, proof)
    return proof


def main():
    require(os.environ['GITHUB_REPOSITORY'] == REPOSITORY and os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch'
            and os.environ['GITHUB_REF_NAME'] == 'codex/agentready-runtime', 'FORK_CANONICAL_MANUAL_AUTHORITY_REQUIRED')
    revision, private_guard = os.environ['RETIREMENT_SOURCE_REVISION'], os.environ['PRIVATE_GUARD_SHA256']
    require(re.fullmatch('[a-f0-9]{40}', revision) and revision == os.environ['GITHUB_SHA'], 'REVIEWED_SOURCE_REQUIRED')
    require(re.fullmatch('[a-f0-9]{64}', private_guard), 'ROOT_FRESH_PRIVATE_GUARD_HASH_REQUIRED')
    root = Path(__file__).resolve().parents[2]
    request, expected, request_hash = load_evidence(root)
    output = Path(os.environ['RUNNER_TEMP']) / 'agentsee-public-runtime-retirement'
    output.mkdir(mode=0o700, exist_ok=False)
    proof = {'phase': 'validating_public_scope', 'requestSha256': request_hash, 'retirementSourceRevision': revision,
        'workflowRunId': int(os.environ['GITHUB_RUN_ID']), 'workflowAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
        'proofSha256': {key: value['sha256'] for key, value in request['proofs'].items()},
        'rootFreshPrivateRegistryGuardSha256': private_guard, 'sourceRepository': REPOSITORY,
        'packagePublisherRepository': PUBLISHER, 'deleteAttempted': False}
    path = output / 'retirement-proof.json'; save(path, proof)
    try:
        retire(expected, proof, path)
    except (ValueError, RuntimeError) as error:
        proof.update(phase='failed', code=str(error)); save(path, proof)
        raise
    print(json.dumps({'phase': 'complete', 'publicPackageId': PUBLIC_ID, 'publicPackageApi404': True,
                      'privatePackageId': PRIVATE_ID, 'productionChanged': False}))


if __name__ == '__main__':
    main()
