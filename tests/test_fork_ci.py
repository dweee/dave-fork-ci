import copy
import io
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

    def test_workflow_change_keeps_fork_control_files(self):
        candidate = self.candidate(self.upstream('.github/workflows/upstream.yml', 'name: changed\n'))
        self.assertIsNone(ci.tree_bytes(self.repo, candidate, '.github/workflows/upstream.yml', optional=True))
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'local-patch.txt'), b'preserve custom behavior\n')
        ci.verify(self.repo, self.base, candidate, self.base, POLICY)

    def test_clean_protected_edit_stays_on_the_fork_version(self):
        ci.git(self.repo, 'checkout', '--detach', self.base)
        path = self.repo / '.github/CODEOWNERS'
        path.parent.mkdir(parents=True)
        path.write_text('fork\n')
        self.base = self.commit('fork codeowners')
        ci.git(self.repo, 'checkout', '--detach', self.base)
        path.write_text('upstream\n')
        (self.repo / 'upstream.txt').write_text('synced\n')
        target = self.commit('upstream codeowners and source')
        candidate = self.candidate(target)
        self.assertEqual(ci.tree_bytes(self.repo, candidate, '.github/CODEOWNERS'), b'fork\n')
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'upstream.txt'), b'synced\n')

    def test_protected_workflow_conflict_keeps_fork_deletion_and_upstream_source(self):
        ci.git(self.repo, 'checkout', '--detach', self.base)
        path = self.repo / '.github/workflows/tests.yml'
        path.parent.mkdir(parents=True)
        path.write_text('name: old\n')
        shared = self.commit('shared workflow')
        ci.git(self.repo, 'checkout', '--detach', shared)
        path.write_text('name: new\n')
        (self.repo / 'upstream.txt').write_text('synced\n')
        target = self.commit('upstream edits workflow and source')
        ci.git(self.repo, 'checkout', '--detach', shared)
        path.unlink()
        self.base = self.commit('fork removes upstream workflow')
        candidate = self.candidate(target)
        self.assertIsNone(ci.tree_bytes(self.repo, candidate, '.github/workflows/tests.yml', optional=True))
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'upstream.txt'), b'synced\n')
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'local-patch.txt'), b'preserve custom behavior\n')

    def test_new_submodule_transport_is_not_imported(self):
        candidate = self.candidate(self.upstream('.gitmodules', '[submodule "x"]\n'))
        self.assertIsNone(ci.tree_bytes(self.repo, candidate, '.gitmodules', optional=True))
        self.assertEqual(ci.tree_bytes(self.repo, candidate, 'local-patch.txt'), b'preserve custom behavior\n')
        ci.verify(self.repo, self.base, candidate, self.base, POLICY)

    def test_symlink_state_directory_cannot_escape(self):
        ci.git(self.repo, 'checkout', '--detach', self.upstream_base)
        (self.repo / '.fork-ci').symlink_to(self.tmp.name, target_is_directory=True)
        target = self.commit('upstream state symlink')
        outside = Path(self.tmp.name) / 'upstream-state.json'
        candidate = self.candidate(target)
        self.assertFalse(outside.exists())
        self.assertFalse((self.repo / '.fork-ci').is_symlink())
        self.assertEqual(ci.text(self.repo, 'ls-tree', candidate, '--', '.fork-ci').split(' ', 1)[0], '040000')
        self.assertTrue((self.repo / ci.STATE).is_file())

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


class MetadataTests(unittest.TestCase):
    def test_release_metadata_uses_job_token_only_at_fixed_api_origin(self):
        response = io.BytesIO(b'{"id": 1}')
        with patch.dict(os.environ, {'GH_TOKEN': 'synthetic-job-token'}), patch.object(ci.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            self.assertEqual(ci.api('repos/example/project/releases/latest'), {'id': 1})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.github.com/repos/example/project/releases/latest')
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-job-token')
            self.assertIsInstance(build.call_args.args[0], ci.NoMetadataRedirect)

    def test_release_metadata_refuses_endpoint_escape_before_network_access(self):
        with patch.object(ci.urllib.request, 'build_opener') as build:
            for endpoint in ['https://elsewhere.invalid', '//elsewhere.invalid', 'repos/example/project/releases/latest?extra=1', 'repos/example/project/releases/latest/../secrets']:
                with self.assertRaises(ci.Blocked):
                    ci.api(endpoint)
            build.assert_not_called()

    def test_metadata_redirect_cannot_forward_authorization(self):
        request = ci.urllib.request.Request('https://api.github.com/repos/example/project/releases/latest', headers={'Authorization': 'Bearer synthetic-job-token'})
        with self.assertRaisesRegex(ci.Blocked, 'redirect requires review'):
            ci.NoMetadataRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://elsewhere.invalid')

    def test_metadata_http_failure_never_exposes_token_or_error_body(self):
        failure = ci.urllib.error.HTTPError('https://api.github.com/', 403, 'synthetic-job-token', {}, None)
        with patch.object(ci.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = failure
            with self.assertRaises(ci.Blocked) as result:
                ci.api('repos/example/project/releases/latest')
            self.assertNotIn('synthetic-job-token', str(result.exception))

    def test_metadata_404_still_means_no_release(self):
        with patch.object(ci.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = ci.urllib.error.HTTPError('https://api.github.com/', 404, 'missing', {}, None)
            self.assertIsNone(ci.api('repos/example/project/releases/latest'))


if __name__ == '__main__':
    unittest.main()
