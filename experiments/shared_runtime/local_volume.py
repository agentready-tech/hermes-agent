"""Linux-owned fixture state: SQLite WAL must not depend on a macOS shared-folder VFS."""
import argparse
import hashlib
import json
from pathlib import Path

from experiments.shared_runtime.local_docker import command


def volume_mount(volume, destination, *, subpath=None, readonly=False):
    parts = ['type=volume', 'src=' + volume, 'dst=' + destination]
    if subpath:
        path = Path(subpath)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Owned volume subpath required')
        parts.append('volume-subpath=' + subpath)
    if readonly:
        parts.append('readonly')
    return ','.join(parts)


def prepare(image, identity, output, plan):
    volume = identity + '-state'
    command(['docker', 'volume', 'create', '--label', 'io.agentsee.prototype=' + identity, volume])
    owner = command(['docker', 'volume', 'inspect', '--format',
                     '{{index .Labels "io.agentsee.prototype"}}', volume])
    if owner != identity:
        raise RuntimeError('OWN_STATE_VOLUME_REQUIRED')
    plan.update(stateVolume=volume, stateStorage='docker-linux-volume')
    (output / 'launch.json').write_text(json.dumps(plan, indent=2))
    script = ("from pathlib import Path; import shutil; "
              "root=Path('/probe'); shutil.copyfile('/host/launch.json',root/'launch.json'); "
              "[(root/p/n).mkdir(parents=True,exist_ok=True) "
              "for p in ('home/profiles','workspaces') for n in ('alpha','bravo')]")
    command(['docker', 'run', '--rm', '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
             '--label', 'io.agentsee.prototype=' + identity, '--mount', volume_mount(volume, '/probe'),
             '--mount', f'type=bind,src={output},dst=/host,readonly',
             '--entrypoint', '/opt/hermes/.venv/bin/python', image, '-B', '-c', script])
    return volume


def export(output):
    plan = json.loads((output / 'launch.json').read_text())
    observed = json.loads(command(['docker', 'inspect', '--format', '{{json .}}', plan['containerId']]))
    if (observed['Id'] != plan['containerId'] or observed['Name'] != '/' + plan['harnessContainer']
            or observed['State']['Status'] != 'exited'
            or observed['Config']['Labels'].get('io.agentsee.prototype') != plan['harnessContainer']):
        raise RuntimeError('OWN_EXITED_FIXTURE_REQUIRED')
    volume = json.loads(command(['docker', 'volume', 'inspect', '--format', '{{json .}}', plan['stateVolume']]))
    if volume['Labels'].get('io.agentsee.prototype') != plan['harnessContainer']:
        raise RuntimeError('OWN_STATE_VOLUME_REQUIRED')
    target = output / 'export'
    target.mkdir(mode=0o700)
    script = '''from pathlib import Path
import hashlib,json,shutil,stat
root,target=Path('/probe'),Path('/export')
def ignore(directory,names):
    return [n for n in names if n in ('skills','cache','__pycache__')
            or not (stat.S_ISREG((Path(directory)/n).lstat().st_mode)
                    or stat.S_ISDIR((Path(directory)/n).lstat().st_mode)
                    or stat.S_ISLNK((Path(directory)/n).lstat().st_mode))]
shutil.copytree(root,target,dirs_exist_ok=True,symlinks=True,ignore=ignore)
names=[n for n in ('status.json','scripted-proof.json','model-receipts.json') if (root/n).is_file()]
(target/'export-hashes.json').write_text(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names}))
'''
    command(['docker', 'run', '--rm', '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
             '--label', 'io.agentsee.prototype=' + plan['harnessContainer'], '--mount',
             volume_mount(plan['stateVolume'], '/probe', readonly=True), '--mount',
             f'type=bind,src={target},dst=/export', '--entrypoint', '/opt/hermes/.venv/bin/python',
             plan['image'], '-B', '-c', script])
    hashes = json.loads((target / 'export-hashes.json').read_text())
    if not hashes or any(hashlib.sha256((target / n).read_bytes()).hexdigest() != sha for n, sha in hashes.items()):
        raise RuntimeError('EXPORTED_PROOF_HASH_MISMATCH')
    (output / 'export-proof.json').write_text(json.dumps({'hashesVerified': True, 'files': hashes,
        'stateStorage': plan['stateStorage'], 'stateVolumeRetained': True,
        'immutableSkillsAndCachesExcludedFromExport': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['export'])
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    export(args.output.resolve())


if __name__ == '__main__':
    main()
