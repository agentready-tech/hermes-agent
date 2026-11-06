"""Launch a secret-scoped, disposable LOCAL real-provider probe from clean Git."""
import argparse
import json
import os
from pathlib import Path
import uuid

from dotenv import dotenv_values
from experiments.shared_runtime.local_docker import command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--image', required=True)
    parser.add_argument('--source-pointer', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    if command(['git', '-C', str(root), 'status', '--porcelain']):
        raise RuntimeError('CLEAN_COMMITTED_SOURCE_REQUIRED')
    revision = command(['git', '-C', str(root), 'rev-parse', 'HEAD'])
    stamp = command(['docker', 'image', 'inspect', '--format',
                     '{{index .Config.Labels "org.opencontainers.image.revision"}}', args.image])
    if revision != stamp:
        raise RuntimeError('IMAGE_SOURCE_MISMATCH')
    endpoint = command(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'])
    if not endpoint.startswith('unix://'):
        raise RuntimeError('LOCAL_DOCKER_REQUIRED')
    pointer = json.loads(args.source_pointer.read_text())
    key = dotenv_values(pointer['sourcePath']).get(pointer['fieldName'])
    if not key or '\n' in key or not key.startswith('sk-or-'):
        raise RuntimeError('LOCAL_PROVIDER_KEY_UNAVAILABLE')
    model = pointer['suggestedModelFromSameLocalProfile']
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = args.output.resolve()
    (output / 'provider-key.private').write_text(key)
    identity = 'hermes-prototype-paid-' + uuid.uuid4().hex[:10]
    plan = dict(phase='preparing', image=args.image, sourceRevision=revision, model=model,
                harnessContainer=identity, productionTouched=False, nativeStateStorage='container-linux-filesystem')
    (output / 'launch.json').write_text(json.dumps(plan, indent=2))
    cid = command(['docker', 'run', '-d', '--pull=never', '--name', identity,
        '--label', 'io.agentsee.prototype=' + identity, '--cap-drop=ALL', '--ulimit', 'core=0',
        '--security-opt=no-new-privileges', '--mount', f'type=bind,src={output},dst=/probe',
        '--entrypoint', '/opt/hermes/.venv/bin/python', args.image,
        '/opt/hermes/experiments/shared_runtime/paid_probe.py'])
    plan.update(phase='dispatched', containerId=cid)
    (output / 'launch.json').write_text(json.dumps(plan, indent=2))
    print(json.dumps({'phase': 'dispatched', 'container': identity, 'sourceRevision': revision}))


if __name__ == '__main__':
    main()
