"""Copy exact manifests to the private canonical package; never builds or deletes images."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = 'ghcr.io/agentready-tech/agentsee-hermes'
DESTINATION = 'ghcr.io/agentready-tech/agentsee-hermes-runtime'
PACKAGE = 'orgs/agentready-tech/packages/container/agentsee-hermes-runtime'
REPOSITORY = 'agentready-tech/hermes-agent'


def run(args, **kwargs):
    kwargs.setdefault('timeout', 120)
    return subprocess.check_output(args, **kwargs)


def validate(request):
    if request['source'] != SOURCE or request['destination'] != DESTINATION:
        raise ValueError('EXACT_COMMON_REGISTRY_SCOPE_REQUIRED')
    versions = request['versions']
    if not versions or len({v['version'] for v in versions}) != len(versions):
        raise ValueError('UNIQUE_RELEASE_VERSIONS_REQUIRED')
    for row in versions:
        if row['tags'] != [row['version']] or not re.fullmatch(r'sha256:[a-f0-9]{64}', row['digest']):
            raise ValueError('IMMUTABLE_RELEASE_VERSION_REQUIRED')
        if (not re.fullmatch(r'[a-f0-9]{40}', row['sourceRevision'])
                or not re.fullmatch(r'202[0-9]\.[0-9]+\.[0-9]+\.[0-9]+', row['version'])):
            raise ValueError('SOURCE_RELEASE_IDENTITY_REQUIRED')
    if request['activeVersion'] not in {v['version'] for v in versions}:
        raise ValueError('ACTIVE_RELEASE_REQUIRED')


def verify_package(value, expected_repository=REPOSITORY):
    if expected_repository not in (REPOSITORY, 'agentready-tech/AgentReady') or value.get('visibility') != 'private':
        raise ValueError('PRIVATE_FORK_PACKAGE_REQUIRED')
    if (value.get('id') != 16663531 or value.get('name') != 'agentsee-hermes-runtime'
            or (value.get('owner') or {}).get('login') != 'agentready-tech'
            or os.environ.get('GITHUB_REPOSITORY') != 'agentready-tech/AgentReady'):
        raise ValueError('EXACT_PRIVATE_PACKAGE_AND_PUBLISHER_REQUIRED')
    observed_repository = (value.get('repository') or {}).get('full_name')
    if observed_repository is not None and observed_repository != expected_repository:
        raise ValueError('PACKAGE_REPOSITORY_ASSOCIATION_CHANGED')
    return value


def package_metadata(expected_repository=REPOSITORY):
    result = subprocess.run(['gh', 'api', PACKAGE], capture_output=True, check=False, timeout=120)
    if result.returncode:
        if b'HTTP 404' in result.stderr:
            return None
        raise RuntimeError('PACKAGE_METADATA_UNAVAILABLE')
    value = json.loads(result.stdout)
    return verify_package(value, expected_repository)


def source_identity(row):
    source = SOURCE + '@' + row['digest']
    info = json.loads(run(['skopeo', 'inspect', 'docker://' + source]))
    labels = info['Labels']
    if (info['Digest'] != row['digest']
            or labels.get('org.opencontainers.image.revision') != row['sourceRevision']
            or labels.get('org.opencontainers.image.version') != row['version']
            or labels.get('org.opencontainers.image.source') != 'https://github.com/' + REPOSITORY):
        raise ValueError('SOURCE_MANIFEST_LABELS_MISMATCH')
    return source


def target_exists(row, package_repository=REPOSITORY):
    # GHCR may return 403 for an uncreated repository. API404 proves package
    # absence; a registry403 on an existing package remains an uncertain outcome.
    if package_metadata(package_repository) is None:
        return False
    target = DESTINATION + ':' + row['version']
    result = subprocess.run(['skopeo', 'inspect', 'docker://' + target], capture_output=True, check=False, timeout=120)
    if not result.returncode:
        if json.loads(result.stdout)['Digest'] != row['digest']:
            raise ValueError('DESTINATION_TAG_CONFLICT')
        return True
    if any(value in result.stderr.lower() for value in (b'manifest unknown', b'name unknown', b'404')):
        return False
    raise ValueError('DESTINATION_LOOKUP_UNCERTAIN')


def copy_version(row, package_repository=REPOSITORY):
    source = source_identity(row)
    target = DESTINATION + ':' + row['version']
    if not target_exists(row, package_repository):
        subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests', 'docker://' + source,
                        'docker://' + target], check=True, timeout=900)
    if package_metadata(package_repository) is None:
        raise ValueError('PRIVATE_PACKAGE_REGISTRATION_MISSING')
    observed = json.loads(run(['skopeo', 'inspect', 'docker://' + target]))
    if observed['Digest'] != row['digest']:
        raise ValueError('MANIFEST_CHANGED')
    return {**row, 'privateImage': DESTINATION + '@' + row['digest']}


def verify_private_pull(image, output):
    layout = output / 'verification'
    subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests', 'docker://' + image,
                    'oci:' + str(layout) + ':verified'], check=True, timeout=900)
    index = json.loads((layout / 'index.json').read_text())
    if len(index['manifests']) != 1 or index['manifests'][0]['digest'] != image.split('@')[1]:
        raise ValueError('PRIVATE_PULL_MANIFEST_MISMATCH')
    for blob in (layout / 'blobs/sha256').iterdir():
        with blob.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != blob.name:
                raise ValueError('REGISTRY_BLOB_MISMATCH')


def main():
    if os.environ['GITHUB_REPOSITORY'] != REPOSITORY:
        raise ValueError('FORK_REQUIRED')
    root = Path(__file__).resolve().parents[2]
    request = json.loads((root / 'deploy/kubernetes/runtime-private-request.json').read_text())
    validate(request)
    output = Path(os.environ['RUNNER_TEMP']) / 'agentsee-private-runtime'
    output.mkdir(exist_ok=False)
    run(['skopeo', 'login', 'ghcr.io', '--username', os.environ['GITHUB_ACTOR'], '--password-stdin'],
        input=os.environ['GH_TOKEN'].encode())
    copied = []
    for row in request['versions']:
        copied.append(copy_version(row))
        (output / 'private-copy-proof.json').write_text(json.dumps({'phase': 'copying', 'versions': copied}, indent=2))
    active = next(row for row in copied if row['version'] == request['activeVersion'])
    verify_private_pull(active['privateImage'], output)
    proof = {'phase': 'complete', 'source': SOURCE, 'destination': DESTINATION, 'versions': copied,
        'activeVersion': request['activeVersion'], 'activeImage': active['privateImage'],
        'activeSourceRevision': active['sourceRevision'], 'activeRegistryFullPullVerified': True,
        'private': True, 'repository': REPOSITORY, 'noRebuild': True, 'publicPackageDeleted': False,
        'workflowRunId': int(os.environ['GITHUB_RUN_ID']), 'workflowAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
        'originalBuildRunId': request['originalBuildRunId']}
    (output / 'private-copy-proof.json').write_text(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
