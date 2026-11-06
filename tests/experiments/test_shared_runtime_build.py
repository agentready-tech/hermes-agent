"""A private launcher's umask must not change the committed build input modes."""
import os
import stat
import subprocess

from experiments.shared_runtime.build import export_source


def test_git_export_preserves_modes_and_excludes_untracked_files_under_private_umask(tmp_path):
    repo, output = tmp_path / 'repo', tmp_path / 'output'
    repo.mkdir()
    output.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    ordinary, executable = repo / 'dependency.lock', repo / 'entry.sh'
    ordinary.write_text('immutable dependency input\n')
    ordinary.chmod(0o644)
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'Build fixture'], check=True)
    (repo / 'untracked-secret.env').write_text('not part of the image\n')
    previous = os.umask(0o077)
    try:
        source = export_source(repo, 'HEAD', output)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((source / ordinary.name).stat().st_mode) == 0o644
    assert stat.S_IMODE((source / executable.name).stat().st_mode) == 0o755
    assert (source / ordinary.name).read_bytes() == ordinary.read_bytes()
    assert not (source / 'untracked-secret.env').exists()


def test_stamp_subprocess_does_not_add_host_bytecode_to_export(tmp_path):
    from experiments.shared_runtime.build import stamp_source
    (tmp_path / 'pm').mkdir()
    (tmp_path / 'pm/__init__.py').write_text('VALUE = 1\n')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/write_install_stamp.py').write_text(
        'import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\n'
        'import pm\nPath(sys.argv[sys.argv.index("--output")+1]).write_text("{}")\n')
    stamp_source(tmp_path, 'fixture-revision', '2026.11.6.prototype.10')
    assert (tmp_path / 'install-stamp.json').is_file()
    assert not list(tmp_path.rglob('*.pyc'))


def test_dependency_fingerprint_changes_only_for_native_build_inputs(tmp_path):
    import io
    import tarfile
    from experiments.shared_runtime.dependency_cache import INPUTS, dependency_inputs

    def archive(name, *, dependency='pinned', application='different application'):
        path = tmp_path / name
        files = {key: b'unchanged input' for key in INPUTS}
        files.update({'pm/lock.json': dependency.encode(), 'agent/runtime.py': application.encode()})
        with tarfile.open(path, 'w') as output:
            for key, content in files.items():
                entry = tarfile.TarInfo(key)
                entry.mode, entry.size = 0o644, len(content)
                output.addfile(entry, io.BytesIO(content))
        return path

    first = dependency_inputs(archive('first.tar'))
    assert first == dependency_inputs(archive('application-only.tar', application='new application'))
    assert first != dependency_inputs(archive('new-lock.tar', dependency='changed dependency'))
