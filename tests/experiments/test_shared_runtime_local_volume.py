"""Linux fixture mounts expose one agent's directories; early process death must fail immediately."""
from types import SimpleNamespace

import pytest

from experiments.shared_runtime.local_volume import volume_mount
from experiments.shared_runtime.image_client import Workers


@pytest.mark.parametrize('path', ['/other', '../bravo', 'home/../../bravo'])
def test_owned_volume_subpaths_cannot_escape_the_selected_directory(path):
    with pytest.raises(ValueError, match='Owned volume subpath'):
        volume_mount('owned-fixture', '/agent', subpath=path, readonly=True)


def test_agent_mount_does_not_expose_the_shared_parent():
    mount = dict(part.split('=', 1) if '=' in part else (part, True) for part in
                 volume_mount('owned-fixture', '/agent', subpath='home/profiles/alpha', readonly=True).split(','))
    assert mount['type'] == 'volume' and mount['volume-subpath'] == 'home/profiles/alpha'
    assert mount['dst'] == '/agent' and mount['readonly'] is True


def test_native_sigbus_is_reported_without_waiting_for_a_completion(tmp_path):
    workers = Workers(tmp_path, 'shared', tmp_path)
    workers.processes = [SimpleNamespace(poll=lambda: -7)]
    with pytest.raises(RuntimeError, match='NATIVE_GATEWAY_EXIT:-7'):
        workers.check_alive()


@pytest.mark.parametrize('corrupted', [False, True])
def test_export_verifies_copied_evidence_before_reporting_success(tmp_path, monkeypatch, corrupted):
    import hashlib
    import json
    from experiments.shared_runtime import local_volume

    plan = dict(containerId='fixture-cid', harnessContainer='fixture-owner', stateVolume='fixture-volume',
                stateStorage='docker-linux-volume', image='fixture-image')
    (tmp_path / 'launch.json').write_text(json.dumps(plan))
    original = b'{"phase":"complete"}'

    def command(args):
        if args[1] == 'inspect':
            return json.dumps({'Id': 'fixture-cid', 'Name': '/fixture-owner', 'State': {'Status': 'exited'},
                               'Config': {'Labels': {'io.agentsee.prototype': 'fixture-owner'}}})
        if args[1] == 'volume':
            return json.dumps({'Labels': {'io.agentsee.prototype': 'fixture-owner'}})
        (tmp_path / 'export/status.json').write_bytes(b'changed' if corrupted else original)
        (tmp_path / 'export/export-hashes.json').write_text(json.dumps({'status.json': hashlib.sha256(original).hexdigest()}))
        return ''

    monkeypatch.setattr(local_volume, 'command', command)
    if corrupted:
        with pytest.raises(RuntimeError, match='EXPORTED_PROOF_HASH_MISMATCH'):
            local_volume.export(tmp_path)
        assert not (tmp_path / 'export-proof.json').exists()
    else:
        local_volume.export(tmp_path)
        assert json.loads((tmp_path / 'export-proof.json').read_text())['hashesVerified'] is True
