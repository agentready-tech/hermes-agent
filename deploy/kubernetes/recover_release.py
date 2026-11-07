"""Recover an already built immutable release into the canonical private package."""
from __future__ import annotations

import json
import os
import re
import subprocess

if __package__:
    from .publish_release import IMAGE, ROOT, output_root, phase, run, verify_registry_pull
else:
    from publish_release import IMAGE, ROOT, output_root, phase, run, verify_registry_pull


def package(name):
    result = subprocess.run(['gh', 'api', 'orgs/agentready-tech/packages/container/' + name],
                            capture_output=True, check=False, timeout=120)
    if result.returncode:
        if b'HTTP 404' in result.stderr:
            return None
        raise RuntimeError('PACKAGE_METADATA_UNAVAILABLE')
    return json.loads(result.stdout)


def private_package(value):
    return (value is not None and value.get('visibility') == 'private'
            and (value.get('repository') or {}).get('full_name') == 'agentready-tech/hermes-agent')


def validate_request(request):
    if not re.fullmatch(r'[a-f0-9]{40}', request['sourceRevision']):
        raise ValueError('INVALID_SOURCE_REVISION')
    if request['destination'] != IMAGE or request['source'] != 'ghcr.io/agentready-tech/hermes-agent':
        raise ValueError('UNEXPECTED_REGISTRY_RECOVERY_SCOPE')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', request['digest']):
        raise ValueError('INVALID_SOURCE_DIGEST')
    if not re.fullmatch(r'[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\.[1-9][0-9]*', request['version']):
        raise ValueError('INVALID_RELEASE_VERSION')


def login():
    directory = output_root() / 'auth'
    directory.mkdir(mode=0o700)
    os.environ['DOCKER_CONFIG'] = str(directory)
    os.environ['REGISTRY_AUTH_FILE'] = str(directory / 'registry-auth.json')
    token, actor = os.environ['GH_TOKEN'].encode(), os.environ['GITHUB_ACTOR']
    run(['docker', 'login', 'ghcr.io', '--username', actor, '--password-stdin'], data=token)
    run(['skopeo', 'login', '--username', actor, '--password-stdin', 'ghcr.io'], data=token)


def copy_existing(request):
    phase('verifying_existing_publication')
    source = request['source'] + '@' + request['digest']
    inspection = json.loads(run(['skopeo', 'inspect', 'docker://' + source]))
    labels = inspection['Labels']
    if (inspection['Digest'] != request['digest']
            or labels.get('org.opencontainers.image.revision') != request['sourceRevision']
            or labels.get('org.opencontainers.image.version') != request['version']
            or labels.get('org.opencontainers.image.source') != 'https://github.com/agentready-tech/hermes-agent'):
        raise ValueError('EXISTING_IMAGE_PROVENANCE_MISMATCH')
    destination = IMAGE + ':' + request['version']
    before = package(IMAGE.rsplit('/', 1)[1])
    if before is not None:
        if not private_package(before):
            raise ValueError('DESTINATION_MUST_BE_PRIVATE_AND_FORK_OWNED')
        lookup = subprocess.run(['skopeo', 'inspect', 'docker://' + destination],
                                capture_output=True, check=False, timeout=120)
        if not lookup.returncode:
            if json.loads(lookup.stdout)['Digest'] != request['digest']:
                raise ValueError('IMMUTABLE_DESTINATION_TAG_CONFLICT')
            return IMAGE + '@' + request['digest']
        if not any(v in lookup.stderr.lower() for v in (b'manifest unknown', b'name unknown', b'404', b'not found')):
            raise ValueError('DESTINATION_TAG_LOOKUP_UNCERTAIN')
    phase('copying_same_manifest_to_private_package')
    run(['skopeo', 'copy', '--all', '--preserve-digests', 'docker://' + source,
         'docker://' + destination], log='registry-copy.log')
    observed = package(IMAGE.rsplit('/', 1)[1])
    facts = {'name': (observed or {}).get('name'), 'visibility': (observed or {}).get('visibility'),
             'repository': ((observed or {}).get('repository') or {}).get('full_name')}
    (output_root() / 'package-metadata.json').write_text(json.dumps(facts, indent=2))
    if not private_package(observed):
        raise ValueError('DESTINATION_PRIVACY_OR_REPOSITORY_BINDING_FAILED')
    digest = run(['skopeo', 'inspect', '--format', '{{.Digest}}', 'docker://' + destination]).decode().strip()
    if digest != request['digest']:
        raise ValueError('RECOVERY_CHANGED_IMAGE_MANIFEST')
    return IMAGE + '@' + digest


def verify_source(image, revision):
    phase('verifying_published_source_without_network')
    run(['docker', 'pull', image], log='registry-pull.log')
    proof = json.loads(run(['docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', '/opt/hermes/.venv/bin/python', image,
        '/opt/hermes/deploy/kubernetes/release_source.py', 'verify', '/opt/hermes']))
    if proof.get('sourceRevision') != revision or proof.get('cleanSourceVerified') is not True:
        raise ValueError('RECOVERED_SOURCE_PROOF_MISMATCH')
    (output_root() / 'source-proof.json').write_text(json.dumps(proof, indent=2))
    return proof


def retire_public_attempt(request):
    phase('retiring_only_failed_public_version')
    resource = 'orgs/agentready-tech/packages/container/hermes-agent/versions/' + str(request['publicVersionId'])
    value = json.loads(run(['gh', 'api', resource]))
    if value['name'] != request['digest'] or value['metadata']['container']['tags'] != [request['version']]:
        raise ValueError('PUBLIC_VERSION_DELETE_SCOPE_CHANGED')
    run(['gh', 'api', '--method', 'DELETE', resource])
    result = subprocess.run(['gh', 'api', resource], capture_output=True, check=False, timeout=120)
    if not result.returncode or b'HTTP 404' not in result.stderr:
        raise ValueError('PUBLIC_VERSION_RETIREMENT_UNCONFIRMED')
    return {'versionId': request['publicVersionId'], 'manifestDigest': request['digest'],
            'onlyFailedPublicVersionDeleted': True, 'privateCommonImagePreserved': True}


def main():
    os.umask(0o077)
    if os.environ.get('GITHUB_REPOSITORY') != 'agentready-tech/hermes-agent':
        raise ValueError('FORK_REPOSITORY_REQUIRED')
    output_root().mkdir(exist_ok=False)
    request = json.loads((ROOT / 'deploy/kubernetes/release-recovery-request.json').read_text())
    validate_request(request)
    revision, version = request['sourceRevision'], request['version']
    tag = 'agentsee-v' + version
    if run(['git', 'rev-parse', tag + '^{commit}']).decode().strip() != revision:
        raise ValueError('IMMUTABLE_SOURCE_TAG_MISMATCH')
    run(['git', 'merge-base', '--is-ancestor', revision, 'origin/codex/agentready-runtime'])
    login()
    image = copy_existing(request)
    proof = verify_source(image, revision)
    pull_proof = verify_registry_pull(image, IMAGE + ':' + version)
    retired = retire_public_attempt(request)
    receipt = {'phase': 'complete', 'version': version, 'gitTag': tag, 'sourceRevision': revision,
        'image': image, 'tag': IMAGE + ':' + version, 'platform': 'linux/amd64',
        'workflowRunId': int(os.environ['GITHUB_RUN_ID']), 'workflowAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
        'buildWorkflowRunId': request['buildWorkflowRunId'], 'recoveryPublisherRevision': os.environ['GITHUB_SHA'],
        'commonRuntimeForAllAgents': True, 'fullSourceBuild': True, 'private': True,
        **pull_proof, 'sourceProof': proof,
        'productionTestsPerformed': False, 'agentConfigurationIncluded': False,
        'recoveredWithoutRebuild': True, 'publicAttemptRetirement': retired}
    (output_root() / 'release-proof.json').write_text(json.dumps(receipt, indent=2))
    phase('complete', image=image, version=version)


if __name__ == '__main__':
    main()
