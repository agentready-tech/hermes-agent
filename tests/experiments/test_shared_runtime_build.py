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
