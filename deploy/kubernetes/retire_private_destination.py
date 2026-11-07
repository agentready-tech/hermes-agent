"""Retire only the unused public destination created by failed private promotion 42373670637."""
import json
import os
from pathlib import Path
import subprocess

REPOSITORY = 'agentready-tech/hermes-agent'
RESOURCE = 'orgs/agentready-tech/packages/container/agentsee-hermes-runtime'
SOURCE_RESOURCE = 'orgs/agentready-tech/packages/container/agentsee-hermes'
DIGEST = 'sha256:e26e25ce43f40c6ffa07a5003c4cb621eb1e978783d95a8e925f829fab13b752'


def validate_destination(package, versions):
    if (package.get('id') != 16663044 or package.get('name') != 'agentsee-hermes-runtime'
            or package.get('visibility') != 'public' or package.get('version_count') != 1
            or (package.get('repository') or {}).get('full_name') != REPOSITORY):
        raise ValueError('FAILED_DESTINATION_PACKAGE_SCOPE_CHANGED')
    if (len(versions) != 1 or versions[0].get('id') != 1520165559 or versions[0].get('name') != DIGEST
            or versions[0].get('metadata', {}).get('container', {}).get('tags') != ['2026.11.7.5']):
        raise ValueError('FAILED_DESTINATION_VERSION_SCOPE_CHANGED')


def api(path, method='GET'):
    result = subprocess.run(['gh', 'api', '--method', method, path], capture_output=True, timeout=60, check=False)
    if result.returncode:
        if method == 'GET' and b'HTTP 404' in result.stderr:
            return None
        raise RuntimeError('SCOPED_PACKAGE_' + method + '_FAILED')
    return json.loads(result.stdout) if result.stdout.strip() else None


def source_snapshot():
    package = api(SOURCE_RESOURCE)
    versions = api(SOURCE_RESOURCE + '/versions?per_page=100')
    request = json.loads(Path(__file__).with_name('runtime-private-request.json').read_text())
    actual = {(row['name'], tuple(row['metadata']['container']['tags'])) for row in versions}
    expected = {(row['digest'], tuple(row['tags'])) for row in request['versions']}
    if package['id'] != 16640069 or package['version_count'] != 5 or actual != expected:
        raise ValueError('ORIGINAL_PUBLIC_SOURCE_RELEASES_CHANGED')
    return {'id': package['id'], 'versions': sorted(actual)}


def retire():
    source = source_snapshot()
    package = api(RESOURCE)
    if package is not None:
        validate_destination(package, api(RESOURCE + '/versions?per_page=100'))
        api(RESOURCE, method='DELETE')
    if api(RESOURCE) is not None or source_snapshot() != source:
        raise ValueError('SCOPED_RETIREMENT_NOT_CONFIRMED')
    return {'phase': 'complete', 'destinationPackageId': 16663044, 'destinationVersionId': 1520165559,
        'destinationName': 'agentsee-hermes-runtime', 'digest': DIGEST, 'tags': ['2026.11.7.5'],
        'onlyFailedUnusedDestinationDeleted': True, 'destinationApi404': True,
        'originalPublicSourcePackageId': 16640069, 'originalFiveReleasesPreserved': True,
        'productionChanged': False, 'imagesBuiltOrDownloaded': False}


def main():
    if os.environ['GITHUB_REPOSITORY'] != REPOSITORY:
        raise ValueError('SOURCE_FORK_PACKAGE_AUTHORITY_REQUIRED')
    root = Path(os.environ['RUNNER_TEMP']) / 'failed-private-destination'
    root.mkdir(mode=0o700, exist_ok=False)
    proof = retire()
    proof.update(workflowRunId=int(os.environ['GITHUB_RUN_ID']),
                 workflowAttempt=int(os.environ['GITHUB_RUN_ATTEMPT']))
    (root / 'cleanup-proof.json').write_text(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
