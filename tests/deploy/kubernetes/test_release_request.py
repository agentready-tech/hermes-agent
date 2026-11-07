"""Actual Git tag/ancestry checks cannot substitute a publisher commit or an unpushed branch."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy.kubernetes import publish_release as publish


class ReleaseRequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.repo = root / 'source'; self.repo.mkdir()
        self.remote = root / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True, timeout=15)
        self.git('init', '-q', '-b', 'codex/agentready-runtime')
        self.git('config', 'user.name', 'Release fixture')
        self.git('config', 'user.email', 'release@example.invalid')
        (self.repo / 'module.py').write_text('pass\n')
        self.git('add', 'module.py'); self.git('commit', '-qm', 'Source')
        canonical = 'https://github.com/' + publish.SOURCE_REPOSITORY + '.git'
        self.git('remote', 'add', 'origin', canonical)
        self.git('config', 'url.' + str(self.remote) + '.insteadOf', canonical)
        self.git('push', '-qu', 'origin', 'HEAD')
        self.revision = self.git('rev-parse', 'HEAD')
        self.tag = 'agentsee-v2026.11.7.6'
        self.git('tag', self.tag); self.git('push', '-q', 'origin', 'refs/tags/' + self.tag)
        self.environment = {'GITHUB_REPOSITORY': publish.PUBLISHER_REPOSITORY,
            'GITHUB_SHA': 'b' * 40, 'AGENTSEE_SOURCE_TAG': self.tag, 'AGENTSEE_SOURCE_REVISION': self.revision}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       text=True, stderr=subprocess.DEVNULL, timeout=15).strip()

    def request(self, **environment):
        with patch.object(publish, 'ROOT', self.repo), patch.dict(os.environ, {**self.environment, **environment}):
            return publish.release_request()

    def test_pushed_lightweight_and_annotated_tags_identify_fork_not_publisher(self):
        self.assertEqual(self.request(), (self.revision, '2026.11.7.6'))
        self.git('tag', '-d', self.tag)
        self.git('tag', '-a', self.tag, '-m', 'Annotated source release')
        self.git('push', '-qf', 'origin', 'refs/tags/' + self.tag)
        self.assertEqual(self.request(), (self.revision, '2026.11.7.6'))

    def test_branch_short_commit_public_publisher_and_foreign_fork_are_refused(self):
        for changed in ({'AGENTSEE_SOURCE_TAG': 'main'}, {'AGENTSEE_SOURCE_TAG': 'codex/agentready-runtime'},
                        {'AGENTSEE_SOURCE_REVISION': self.revision[:12]},
                        {'GITHUB_REPOSITORY': publish.SOURCE_REPOSITORY}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.request(**changed)
        self.git('config', 'remote.origin.url', 'https://github.com/foreign/hermes-agent.git')
        with self.assertRaisesRegex(ValueError, 'canonical Hermes fork'):
            self.request()

    def test_unpushed_or_remotely_moved_tag_is_refused(self):
        self.git('push', '-q', 'origin', ':refs/tags/' + self.tag)
        with self.assertRaisesRegex(ValueError, 'already be pushed'):
            self.request()
        self.git('commit', '--allow-empty', '-qm', 'Another source commit')
        changed = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'HEAD')
        self.git('tag', '-f', self.tag); self.git('push', '-q', 'origin', 'refs/tags/' + self.tag)
        self.git('tag', '-f', self.tag, self.revision); self.git('checkout', '-q', '--detach', self.revision)
        self.assertNotEqual(changed, self.revision)
        with self.assertRaisesRegex(ValueError, 'already be pushed'):
            self.request()

    def test_tagged_commit_must_belong_to_canonical_working_branch(self):
        self.git('commit', '--allow-empty', '-qm', 'Unmerged work')
        changed = self.git('rev-parse', 'HEAD')
        self.git('tag', '-f', self.tag); self.git('push', '-qf', 'origin', 'refs/tags/' + self.tag)
        with self.assertRaises(subprocess.CalledProcessError):
            self.request(AGENTSEE_SOURCE_REVISION=changed)


if __name__ == '__main__':
    unittest.main()
