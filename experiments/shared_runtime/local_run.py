"""Launch one disposable, labelled LOCAL experiment from its exact clean Git image."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import uuid


def command(args):
    return subprocess.check_output(args, text=True, timeout=45).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    if command(['git', '-C', str(root), 'status', '--porcelain']):
        raise RuntimeError('CLEAN_COMMITTED_SOURCE_REQUIRED')
    revision = command(['git', '-C', str(root), 'rev-parse', 'HEAD'])
    image_revision = command(['docker', 'image', 'inspect', '--format',
        '{{index .Config.Labels "org.opencontainers.image.revision"}}', args.image])
    if revision != image_revision:
        raise RuntimeError('IMAGE_SOURCE_MISMATCH')
    endpoint = command(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'])
    if not endpoint.startswith('unix://'):
        raise RuntimeError('LOCAL_DOCKER_SOCKET_REQUIRED')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = args.output.resolve()
    identity = 'hermes-prototype-' + uuid.uuid4().hex[:10]
    sandboxes = {name: identity + '-' + name for name in ('alpha', 'bravo')}
    launch = dict(image=args.image, sourceRevision=revision, sandboxes=sandboxes,
                  harnessContainer=identity, productionTouched=False, phase='preparing')
    (output / 'launch.json').write_text(json.dumps(launch, indent=2))
    created = []
    try:
        for name, container in sandboxes.items():
            home, workspace = output / 'home' / 'profiles' / name, output / 'workspaces' / name
            home.mkdir(parents=True)
            workspace.mkdir(parents=True)
            command(['docker', 'run', '-d', '--pull=never', '--name', container,
                '--label', 'io.agentsee.prototype=' + identity, '--network=none', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--mount', f'type=bind,src={home},dst=/agent,readonly',
                '--mount', f'type=bind,src={workspace},dst=/workspace', '--entrypoint', '/bin/sleep',
                args.image, 'infinity'])
            created.append(container)
        cid = command(['docker', 'run', '-d', '--pull=never', '--name', identity,
            '--label', 'io.agentsee.prototype=' + identity, '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--mount', f'type=bind,src={output},dst=/probe',
            '--mount', f'type=bind,src={endpoint.removeprefix("unix://")},dst=/var/run/docker.sock',
            '--entrypoint', '/opt/hermes/.venv/bin/python', args.image,
            '/opt/hermes/experiments/shared_runtime/image_probe.py'])
        launch.update(phase='dispatched', containerId=cid)
        (output / 'launch.json').write_text(json.dumps(launch, indent=2))
        print(json.dumps(dict(phase='dispatched', container=identity, sourceRevision=revision)))
    except Exception:
        launch['phase'] = 'dispatch_failed'
        (output / 'launch.json').write_text(json.dumps(launch, indent=2))
        for container in created:
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=30, check=False)
        raise


if __name__ == '__main__':
    main()
