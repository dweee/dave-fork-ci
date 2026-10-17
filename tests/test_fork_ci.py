import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import fork_ci as ci

POLICY = {'version': 1, 'upstream': 'example/project', 'branch': 'main', 'track': 'both',
          'author': {'name': 'dave', 'email': 'git@d.monke.fail'},
          'required_paths': ['local-patch.txt', 'nix/package.nix'],
          'matrix': [{'system': 'x86_64-linux', 'packages': ['default'], 'checks': [], 'tests': []}]}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        ci.git(self.repo, 'init', '--quiet')
        ci.git(self.repo, 'config', 'user.name', 'dave')
        ci.git(self.repo, 'config', 'user.email', 'git@d.monke.fail')
        (self.repo / 'upstream.txt').write_text('base\n')
        self.upstream_base = self.commit('upstream base')
        (self.repo / 'flake.nix').write_text('{}\n')
        (self.repo / '.fork-ci.json').write_text(json.dumps(POLICY))
        (self.repo / 'local-patch.txt').write_text('preserve custom behavior\n')
        (self.repo / 'nix').mkdir()
        (self.repo / 'nix/package.nix').write_text('{}\n')
        self.base = self.commit('fork packaging and custom behavior')

    def commit(self, message):
        ci.git(self.repo, 'add', '--all')
        ci.git(self.repo, 'commit', '--no-gpg-sign', '-qm', message)
        return ci.text(self.repo, 'rev-parse', 'HEAD')

    def upstream(self, path='upstream.txt', contents='new upstream\n'):
        ci.git(self.repo, 'checkout', '--detach', self.upstream_base)
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(contents)
        return self.commit('upstream update')

    def candidate(self, target):
        state = {'version': 1, 'upstream': 'example/project', 'track': 'both',
                 'branch': target, 'release': None}
        return ci.merge_candidate(self.repo, self.base, [target], POLICY, state)

    def test_three_way_merge_preserves_fork_history_and_custom_changes(self):
        target = self.upstream()
        candidate = self.candidate(target)
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'local-patch.txt'), b'preserve custom behavior\n')
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'upstream.txt'), b'new upstream\n')
        ci.verify(self.repo, self.base, candidate, self.base, POLICY)
        self.assertEqual(ci.text(self.repo, 'show', '-s', '--format=%an <%ae>', candidate), 'dave <git@d.monke.fail>')
        self.assertEqual(ci.text(self.repo, 'rev-parse', candidate + '~2'), self.base)

    def test_promotion_rejects_main_advanced_since_build(self):
        candidate = self.candidate(self.upstream())
        with self.assertRaises(ci.Blocked):
            ci.verify(self.repo, self.base, candidate, 'f' * 40, POLICY)

    def test_conflicting_local_patch_is_not_overwritten(self):
        ci.git(self.repo, 'checkout', '--detach', self.base)
        (self.repo / 'upstream.txt').write_text('local divergence\n')
        self.base = self.commit('local behavior')
        with self.assertRaises(ci.Blocked):
            self.candidate(self.upstream())

    def test_workflow_change_requires_review(self):
        with self.assertRaises(ci.Blocked):
            self.candidate(self.upstream('.github/workflows/upstream.yml', 'name: changed\n'))

    def test_new_submodule_transport_requires_review(self):
        with self.assertRaises(ci.Blocked):
            self.candidate(self.upstream('.gitmodules', '[submodule "x"]\n'))

    def test_symlink_state_directory_cannot_escape(self):
        ci.git(self.repo, 'checkout', '--detach', self.upstream_base)
        (self.repo / '.fork-ci').symlink_to(self.tmp.name, target_is_directory=True)
        target = self.commit('upstream state symlink')
        with self.assertRaises(ci.Blocked):
            self.candidate(target)

    def test_removed_custom_path_is_blocked(self):
        ci.git(self.repo, 'checkout', '--detach', self.base)
        (self.repo / 'local-patch.txt').unlink()
        head = self.commit('remove required custom behavior marker')
        with self.assertRaises(ci.Blocked):
            ci.guard(self.repo, self.base, head, POLICY)

    def test_candidate_must_contain_recorded_upstream(self):
        candidate = self.candidate(self.upstream())
        value = json.loads(ci.tree_bytes(self.repo, candidate, ci.STATE))
        value['branch'] = '0' * 40
        (self.repo / ci.STATE).write_text(json.dumps(value))
        bad = self.commit('invalid receipt')
        with self.assertRaises(ci.Blocked):
            ci.verify(self.repo, self.base, bad, self.base, POLICY)

    def test_identical_candidate_is_deterministic(self):
        target = self.upstream()
        first = self.candidate(target)
        second = self.candidate(target)
        self.assertEqual(first, second)


class PolicyTests(unittest.TestCase):
    def test_shell_in_package_name_is_rejected(self):
        policy = copy.deepcopy(POLICY)
        policy['matrix'][0]['packages'] = ['default; echo invalid']
        with self.assertRaises(ci.Blocked):
            ci.validate_policy(policy)

    def test_unrecognized_platform_is_rejected(self):
        policy = copy.deepcopy(POLICY)
        policy['matrix'][0]['system'] = 'self-hosted'
        with self.assertRaises(ci.Blocked):
            ci.validate_policy(policy)

    def test_explicit_author_is_required(self):
        policy = copy.deepcopy(POLICY)
        policy['author']['email'] = ''
        with self.assertRaises(ci.Blocked):
            ci.validate_policy(policy)

    def test_release_selection_handles_no_release(self):
        with patch.object(ci, 'api', return_value=None):
            self.assertIsNone(ci.latest_release('example/project'))

    def test_prerelease_is_never_automatically_selected(self):
        with patch.object(ci, 'api', return_value={'prerelease': True}):
            with self.assertRaises(ci.Blocked):
                ci.latest_release('example/project')

    def test_tag_metacharacters_are_rejected(self):
        with self.assertRaises(ci.Blocked):
            ci.ref('bad tag\nextra-output=true')


if __name__ == '__main__':
    unittest.main()
