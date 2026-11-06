"""Reuse only a locally built native dependency stage with identical Git inputs."""
import hashlib
import json
import subprocess
import tarfile

INPUTS = frozenset({'Dockerfile', '.dockerignore', 'docker/tini-shim.sh', 'hermes_constants.py',
                   'hermes_cli/__init__.py', 'hermes_cli/runtime_state.py',
                   'scripts/bundles/payload.py', 'pyproject.toml', 'uv.lock'})


def dependency_inputs(archive):
    result = {}
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            name = member.name
            if name not in INPUTS and not name.startswith('pm/'):
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Unsupported dependency input type: ' + name)
            result[name] = {'sha256': hashlib.sha256(stream.extractfile(member).read()).hexdigest(),
                            'executable': bool(member.mode & 0o111)}
    if not INPUTS.issubset(result) or 'pm/lock.json' not in result:
        raise ValueError('Incomplete native dependency inputs')
    return result


def reuse_dependency_stage(previous, current, target, env):
    receipt = json.loads((previous / 'status.json').read_text())
    archive = previous / 'source.tar'
    if receipt.get('phase') != 'image_built' or receipt.get('architecture') != 'linux/arm64':
        raise ValueError('A successful native Linux ARM64 build is required')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != receipt['sourceArchiveSha256']:
        raise ValueError('Previous source archive changed')
    inputs = dependency_inputs(archive)
    if inputs != dependency_inputs(current / 'source.tar'):
        raise ValueError('Native dependency recipe, lock or source inputs differ')
    image = 'agentsee/shared-runtime-deps:' + receipt['version']
    raw = subprocess.check_output(['docker', 'image', 'inspect', '--format',
                                  '{{json .}}', image], env=env, text=True, timeout=30)
    metadata = json.loads(raw)
    identity = metadata['Id']
    if metadata['Architecture'] != 'arm64' or metadata['Os'] != 'linux':
        raise ValueError('Cached dependency architecture differs')
    log = (previous / 'dependencies.log').read_text()
    if not any(marker + identity in log for marker in ('exporting manifest list ', 'writing image ')):
        raise ValueError('Cached dependency image does not match its build log')
    subprocess.run(['docker', 'image', 'tag', identity, target], env=env, check=True, timeout=30)
    actual = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', target],
                                     env=env, text=True, timeout=30).strip()
    if actual != identity:
        raise ValueError('Dependency image identity changed')
    proof = {'sourceRevision': receipt['sourceRevision'], 'imageId': identity,
             'matchedGitInputs': len(inputs), 'contentAndExecutableBitsIdentical': True,
             'sourceArchiveSha256': receipt['sourceArchiveSha256'],
             'nativeDependencyStageOnly': True, 'applicationReleaseUsedAsBase': False}
    (current / 'dependency-cache-proof.json').write_text(json.dumps(proof, indent=2) + '\n')
    return proof
