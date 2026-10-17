import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_fork_ci import POLICY
import fork_ci

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build', ROOT / 'bin/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildAndWorkflowTests(unittest.TestCase):
    def test_build_uses_explicit_system_and_disables_flake_cache_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(POLICY))
            outputs = root / 'outputs.txt'
            argv = ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(outputs)]
            path = '/nix/store/' + 'a' * 32 + '-package'
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, {}, clear=True), \
                 patch.object(build.subprocess, 'check_output', side_effect=lambda args, **kw: b'a' * 40 if args[0] == 'git' else json.dumps([{'outputs': {'out': path}}]).encode()) as call, patch.object(build.subprocess, 'run'):
                build.main()
            args = call.call_args_list[1].args[0]
            self.assertIn('.#packages.x86_64-linux.default', args)
            self.assertIn('--no-update-lock-file', args)
            self.assertIn('--no-write-lock-file', args)
            self.assertIn('accept-flake-config', args)
            self.assertEqual(args[args.index('accept-flake-config') + 1], 'false')
            self.assertEqual(outputs.read_text(), path + '\n')

    def test_build_rejects_cache_upload_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(POLICY))
            with patch.object(sys, 'argv', ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(root / 'out')]), \
                 patch.dict(os.environ, {'ATTIC_TOKEN': 'synthetic-regression-fixture'}, clear=True):
                with self.assertRaises(fork_ci.Blocked):
                    build.main()

    def test_failed_regression_command_never_writes_success_manifest(self):
        import copy
        import subprocess
        policy = copy.deepcopy(POLICY)
        policy['matrix'][0]['tests'] = [['regression-check']]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(policy))
            outputs = root / 'outputs.txt'
            with patch.object(sys, 'argv', ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(outputs)]), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(build.subprocess, 'check_output', return_value=b'[{"outputs":{"out":"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package"}}]'), \
                 patch.object(build.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['regression-check'])):
                with self.assertRaises(subprocess.CalledProcessError):
                    build.main()
            self.assertFalse(outputs.exists())

    def test_tracked_source_mutation_never_writes_success_manifest(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(POLICY))
            outputs = root / 'outputs.txt'
            with patch.object(sys, 'argv', ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(outputs)]), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(build.subprocess, 'check_output', side_effect=lambda args, **kw: b'a' * 40 if args[0] == 'git' else b'[{"outputs":{"out":"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-package"}}]'), \
                 patch.object(build.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['git', 'diff'])):
                with self.assertRaises(subprocess.CalledProcessError):
                    build.main()
            self.assertFalse(outputs.exists())

    def test_workflow_keeps_write_and_upload_credentials_out_of_build_job(self):
        source = (ROOT / 'ci/disabled/upstream-sync.yml').read_text()
        build_job = source.split('\n  build:', 1)[1].split('\n  cache:', 1)[0]
        self.assertNotIn('secrets.', build_job)
        self.assertNotIn('contents: write', build_job)
        self.assertNotIn('GH_TOKEN:', build_job)
        cache_job = source.split('\n  cache:', 1)[1].split('\n  promote:', 1)[0]
        self.assertLess(cache_job.index('Install trusted Attic'), cache_job.index('Import data'))
        self.assertEqual(cache_job.count('secrets.ATTIC_TOKEN'), 1)
        self.assertIn('--outputs attic-transfer/outputs.txt', cache_job)
        promote = source.split('\n  promote:', 1)[1]
        self.assertIn("needs.build.result == 'success'", promote)
        self.assertIn("needs.cache.result == 'success'", promote)
        self.assertIn('vars.FORK_CI_AUTO_MERGE', promote)

    def test_all_external_actions_are_pinned(self):
        import re
        source = (ROOT / 'ci/disabled/upstream-sync.yml').read_text()
        for reference in re.findall(r'uses: (\S+)', source):
            if not reference.startswith('./'):
                self.assertRegex(reference, r'@[0-9a-f]{40}$')


if __name__ == '__main__':
    unittest.main()
