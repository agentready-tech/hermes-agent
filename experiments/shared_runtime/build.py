"""Build a local experiment image only from a clean committed experiment branch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BRANCH = 'codex/shared-hermes-runtime-prototype'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from deploy.kubernetes.release_source import MANIFEST, MODULE_ROOTS, digest, git, source_path

    revision = git(root, 'rev-parse', 'HEAD')
    if git(root, 'branch', '--show-current') != BRANCH:
        raise ValueError('Only the local experiment branch may build this image')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Commit every source change before building')
    if not args.version.startswith('2026.11.6.prototype.') or not args.version.rsplit('.', 1)[-1].isdigit():
        raise ValueError('A versioned local prototype tag is required')
    args.output.mkdir(parents=True, exist_ok=False)
    endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format',
        '{{.Endpoints.docker.Host}}'], text=True, timeout=30).strip()
    if not endpoint.startswith('unix://'):
        raise ValueError('The prototype may use only the local Docker engine')
    # Public dependencies need no registry account. Keep the user's locked
    # macOS keychain and existing Docker credentials outside this build.
    docker_config = args.output / 'docker-config'
    docker_config.mkdir(mode=0o700)
    plugins = Path('/Applications/Docker.app/Contents/Resources/cli-plugins')
    config = {'cliPluginsExtraDirs': [str(plugins)]} if plugins.is_dir() else {}
    (docker_config / 'config.json').write_text(json.dumps(config))
    build_env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TZ') if key in os.environ}
    build_env.update(DOCKER_HOST=endpoint, DOCKER_CONFIG=str(docker_config))
    source = args.output / 'source'
    source.mkdir()

    def status(phase, **data):
        value = dict(phase=phase, sourceRevision=revision, version=args.version, productionTouched=False, **data)
        (args.output / 'status.json').write_text(json.dumps(value, indent=2))

    status('exporting_clean_source')
    with (args.output / 'source.tar').open('wb') as stream:
        subprocess.run(['git', '-C', str(root), 'archive', revision], stdout=stream, check=True, timeout=60)
    subprocess.run(['tar', '-xf', str(args.output / 'source.tar'), '-C', str(source)], check=True, timeout=60)
    names = git(root, 'ls-tree', '-r', '--name-only', revision).splitlines()
    files = {name: digest(source / name) for name in names
             if source_path(name) or (name.startswith('experiments/') and name.endswith('.py'))}
    manifest = dict(schemaVersion=1, sourceRevision=revision, source='clean-git-archive',
                    moduleRoots=sorted(MODULE_ROOTS | {'experiments'}), files=files)
    (source / MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + '\n')
    subprocess.run([sys.executable, str(source / 'scripts/write_install_stamp.py'), '--output',
        str(source / 'install-stamp.json'), '--commit', revision, '--branch', BRANCH,
        '--display-version', args.version, '--source', 'local', '--distribution', 'docker',
        '--update-mechanism', 'external'], check=True, timeout=30)
    dependency = 'agentsee/shared-runtime-deps:' + args.version
    image = 'agentsee/shared-runtime:' + args.version

    def build(phase, command, log):
        status(phase)
        with (args.output / log).open('wb') as stream:
            try:
                result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                        env=build_env, timeout=5400, check=False)
            except subprocess.TimeoutExpired:
                status('failed', lastPhase=phase, log=log, error='build_timeout')
                raise
        if result.returncode:
            status('failed', lastPhase=phase, log=log, exitCode=result.returncode)
            raise RuntimeError('Prototype build failed in ' + phase)

    build('building_native_dependency_stage', ['docker', 'build', '--platform', 'linux/arm64', '--target',
        'python_deps', '--file', str(source / 'Dockerfile'), '--tag', dependency, str(source)], 'dependencies.log')
    build('building_local_prototype_image', ['docker', 'build', '--platform', 'linux/arm64', '--file',
        str(source / 'experiments/shared_runtime/Dockerfile'), '--build-arg', 'DEPENDENCY_IMAGE=' + dependency,
        '--build-arg', 'SOURCE_REVISION=' + revision, '--build-arg', 'PROTOTYPE_VERSION=' + args.version,
        '--tag', image, str(source)], 'image.log')
    identity = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                                       env=build_env, text=True, timeout=30).strip()
    status('image_built', image=image, imageId=identity, architecture='linux/arm64',
           sourceArchiveSha256=hashlib.sha256((args.output / 'source.tar').read_bytes()).hexdigest(),
           committedSourceVerified=True, githubPushDeferred=True, benchmarkComplete=False)


if __name__ == '__main__':
    main()
