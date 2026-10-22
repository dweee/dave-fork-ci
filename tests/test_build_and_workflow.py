import importlib.util
import base64
import json
import os
import subprocess
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


def fail_command(prefix):
    def run(args, **kwargs):
        if args[:len(prefix)] == prefix:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)
    return run


class BuildAndWorkflowTests(unittest.TestCase):
    def test_public_cache_is_used_by_packages_and_development_shell_tests(self):
        endpoint = 'https://cache.example.org/packages'
        key = 'packages:' + base64.b64encode(bytes(range(32))).decode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(POLICY))
            outputs = root / 'outputs.txt'
            path = '/nix/store/' + 'a' * 32 + '-package'
            with patch.object(sys, 'argv', ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(outputs)]), \
                 patch.dict(os.environ, {'ATTIC_SUBSTITUTER': endpoint, 'ATTIC_PUBLIC_KEY': key, 'ATTIC_HTTP2': 'false'}, clear=True), \
                 patch.object(build.subprocess, 'check_output', side_effect=lambda args, **kw: b'a' * 40 if args[0] == 'git' else json.dumps([{'outputs': {'out': path}}]).encode()) as call, \
                 patch.object(build.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                build.main()
            args = call.call_args_list[1].args[0]
            self.assertEqual(args[args.index('extra-substituters') + 1], endpoint)
            self.assertEqual(args[args.index('http2') + 1], 'false')
            env = call.call_args_list[1].kwargs['env']
            self.assertIn('extra-substituters = ' + endpoint, env['NIX_CONFIG'])
            self.assertIn('extra-trusted-public-keys = ' + key, env['NIX_CONFIG'])
            self.assertIn('http2 = false', env['NIX_CONFIG'])
            self.assertNotIn('ATTIC_TOKEN', env)

    def test_cache_transport_setting_rejects_configuration_injection(self):
        key = 'packages:' + base64.b64encode(bytes(range(32))).decode()
        for value in ['false\nrequire-sigs = false', '', '0', 'invalid']:
            with self.subTest(value=value), self.assertRaises(fork_ci.Blocked):
                build.public_cache({'ATTIC_SUBSTITUTER': 'https://cache.example.org',
                                    'ATTIC_PUBLIC_KEY': key, 'ATTIC_HTTP2': value})

    def test_invalid_public_cache_settings_cannot_inject_nix_configuration(self):
        endpoint = 'https://cache.example.org/packages'
        key = 'packages:' + base64.b64encode(bytes(range(32))).decode()
        cases = [('', key), (endpoint, ''), ('http://cache.example.org', key),
                 ('https://user:pass@cache.example.org', key),
                 (endpoint + '?credential=fixture', key), (endpoint + '#fragment', key),
                 (endpoint + '\nrequire-sigs = false', key),
                 ('https://cache.example.org:invalid', key), ('https://[invalid', key),
                 (endpoint, key + '\nrequire-sigs = false'), (endpoint, 'packages:eA==')]
        for url, public_key in cases:
            with self.subTest(url=url, public_key=public_key), self.assertRaises(fork_ci.Blocked):
                build.public_cache({'ATTIC_SUBSTITUTER': url, 'ATTIC_PUBLIC_KEY': public_key})

    def test_build_uses_explicit_system_and_disables_flake_cache_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(POLICY))
            outputs = root / 'outputs.txt'
            argv = ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(outputs)]
            path = '/nix/store/' + 'a' * 32 + '-package'
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, {}, clear=True), \
                 patch.object(build.subprocess, 'check_output', side_effect=lambda args, **kw: b'a' * 40 if args[0] == 'git' else json.dumps([{'outputs': {'out': path}}]).encode()) as call, patch.object(build.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                build.main()
            args = call.call_args_list[1].args[0]
            self.assertIn('.#packages.x86_64-linux.default', args)
            self.assertIn('--no-update-lock-file', args)
            self.assertIn('--no-write-lock-file', args)
            self.assertIn('accept-flake-config', args)
            self.assertEqual(args[args.index('accept-flake-config') + 1], 'false')
            self.assertEqual(outputs.read_text(), path + '\n')

    def test_public_derivation_rejection_blocks_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.fork-ci.json').write_text(json.dumps(POLICY))
            outputs = root / 'outputs.txt'
            def run(args, **kwargs):
                if any(str(arg).endswith('public-closure.py') for arg in args):
                    raise subprocess.CalledProcessError(1, args, stderr='private fixture wiicompiled-disc')
                return subprocess.CompletedProcess(args, 0)
            path = '/nix/store/' + 'a' * 32 + '-package'
            with patch.object(sys, 'argv', ['build', '--source', str(root), '--system', 'x86_64-linux', '--outputs', str(outputs)]), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(build.subprocess, 'check_output', side_effect=lambda args, **kw: b'a' * 40 if args[0] == 'git' else json.dumps([{'outputs': {'out': path}}]).encode()) as built, \
                 patch.object(build.subprocess, 'run', side_effect=run):
                with self.assertRaises(fork_ci.Blocked) as failure:
                    build.main()
            self.assertNotIn('private fixture', str(failure.exception))
            self.assertFalse(outputs.exists())
            self.assertFalse(any(call.args[0][:2] == ['nix', 'build'] for call in built.call_args_list))

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
                 patch.object(build.subprocess, 'run', side_effect=fail_command(['regression-check'])):
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
                 patch.object(build.subprocess, 'run', side_effect=fail_command(['git', 'diff'])):
                with self.assertRaises(subprocess.CalledProcessError):
                    build.main()
            self.assertFalse(outputs.exists())

    def test_workflow_keeps_write_and_upload_credentials_out_of_build_job(self):
        source = (ROOT / '.github/workflows/upstream-sync.yml').read_text()
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

    def test_scanner_keeps_default_rules_and_the_known_registration_fixture(self):
        source = (ROOT / '.github/workflows/upstream-sync.yml').read_text()
        self.assertIn('useDefault = true', source)
        self.assertIn('[allowlist]', source)
        self.assertIn('0123456789abcdef', source)
        self.assertIn('--ignore-gitleaks-allow', source)

    def test_scanner_fnv_exception_requires_the_exact_public_constant_declaration(self):
        import re
        import textwrap
        import tomllib
        source = (ROOT / '.github/workflows/upstream-sync.yml').read_text()
        config = source.split("<< 'EOF'\n", 1)[1].split('          EOF', 1)[0]
        allowlist = tomllib.loads(textwrap.dedent(config))['allowlist']
        self.assertEqual(allowlist['regexTarget'], 'line')
        allowed = lambda line: any(re.search(pattern, line) for pattern in allowlist['regexes'])
        self.assertTrue(allowed('        let mut key = 0x6c62272e07bb014262b821756295c58du128;'))
        self.assertFalse(allowed('        let mut key = 0x6c62272e07bb014262b821756295c58eu128;'))
        self.assertFalse(allowed('        let mut key = "unrelated-fixture-value";'))
        self.assertFalse(allowed('let mut key = 0x6c62272e07bb014262b821756295c58du128; api_key = "unrelated";'))

    def test_all_external_actions_are_pinned(self):
        import re
        source = (ROOT / '.github/workflows/upstream-sync.yml').read_text()
        for reference in re.findall(r'uses: (\S+)', source):
            if not reference.startswith('./'):
                self.assertRegex(reference, r'@[0-9a-f]{40}$')


if __name__ == '__main__':
    unittest.main()
