"""Small-repo template: dirty-checkout evidence, no tracked bytecode, daemon health, bounded retries."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('channel_installer', 'ci/install-channel-automation.py')
channel = load('channel', 'ci/release-channel.py')


class Categories(unittest.TestCase):
    def test_transport_and_daemon_errors_are_retryable(self):
        for text in ["error: opening a connection to remote store 'daemon' previously failed",
                     'error: Cannot connect to socket at /nix/var/nix/daemon-socket/socket',
                     'error: unable to download: Could not resolve host: cache.nixos.org',
                     'error: unable to download: Resolving timed out after 5000 milliseconds',
                     'error: unable to download: HTTP error 503']:
            self.assertIn(channel.failure_category(text), channel.RETRYABLE, text)

    def test_integrity_and_build_errors_are_not_retryable(self):
        for text in ['error: hash mismatch in fixed-output derivation', 'error: path is not signed',
                     'error: HTTP error 401', 'error: builder for x failed with exit code 1',
                     'error: attribute horizon missing']:
            self.assertNotIn(channel.failure_category(text), channel.RETRYABLE, text)


class Retries(unittest.TestCase):
    def failing(self, category, times):
        calls = []
        def attempt():
            calls.append(1)
            if len(calls) <= times:
                raise channel.CommandFailure(category + ' (exit 1)', category)
            return 'ok'
        return calls, attempt

    def test_transport_failure_is_retried_with_a_daemon_check(self):
        calls, attempt = self.failing('transport', 2)
        sleeps = []
        with patch.object(channel, 'ensure_daemon') as ensure:
            self.assertEqual(channel.with_transport_retries(['nix', 'path-info'], attempt, sleeps.append), 'ok')
        self.assertEqual((len(calls), sleeps, ensure.call_count), (3, [20, 60], 2))

    def test_retries_are_bounded(self):
        calls, attempt = self.failing('transport', 99)
        with patch.object(channel, 'ensure_daemon'), self.assertRaises(channel.CommandFailure):
            channel.with_transport_retries(['nix', 'path-info'], attempt, lambda _: None)
        self.assertEqual(len(calls), 1 + len(channel.TRANSPORT_RETRY_DELAYS))

    def test_validation_hash_and_non_nix_commands_are_never_retried(self):
        for category, command in [('source-hash-mismatch', 'nix'), ('evaluation', 'nix'), ('command-failed', 'nix'),
                                  ('transport', 'git')]:
            calls, attempt = self.failing(category, 5)
            with self.assertRaises(channel.CommandFailure):
                channel.with_transport_retries([command, 'x'], attempt, lambda _: None)
            self.assertEqual(len(calls), 1, (category, command))

    def test_builds_only_retry_an_unreachable_daemon(self):
        calls, attempt = self.failing('transport', 5)
        with self.assertRaises(channel.CommandFailure):
            channel.with_transport_retries(['nix', 'build'], attempt, lambda _: None)
        self.assertEqual(len(calls), 1)
        calls, attempt = self.failing('daemon-unavailable', 1)
        with patch.object(channel, 'ensure_daemon'):
            self.assertEqual(channel.with_transport_retries(['nix', 'build'], attempt, lambda _: None), 'ok')
        self.assertEqual(len(calls), 2)


class DaemonHealth(unittest.TestCase):
    def test_not_in_daemon_mode_does_nothing(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(channel, 'daemon_healthy') as healthy:
            os.environ.pop('NIX_REMOTE', None)
            channel.ensure_daemon()
        healthy.assert_not_called()

    def test_healthy_daemon_is_left_alone(self):
        with patch.dict(os.environ, {'NIX_REMOTE': 'daemon'}), patch.object(channel, 'daemon_healthy', return_value=True), \
                patch.object(channel.subprocess, 'Popen') as popen:
            channel.ensure_daemon()
        popen.assert_not_called()

    def test_dead_daemon_that_cannot_restart_fails_clearly(self):
        child = SimpleNamespace(poll=lambda: 1)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'NIX_REMOTE': 'daemon'}), \
                patch.object(channel, 'daemon_healthy', return_value=False), \
                patch.object(channel.subprocess, 'Popen', return_value=child), \
                patch.object(channel, 'DAEMON_SOCKET', Path(directory) / 'missing'), \
                patch.object(channel.time, 'sleep'), patch('builtins.open', create=True):
            with self.assertRaises(channel.CommandFailure) as caught:
                channel.ensure_daemon()
        self.assertEqual(caught.exception.category, 'daemon-unavailable')
        self.assertIn('could not be restarted', str(caught.exception))


class DirtyNames(unittest.TestCase):
    def test_lists_names_only(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                subprocess.run(['git', '-C', directory, '-c', 'user.name=t', '-c', 'user.email=t@t', *args], check=True,
                               capture_output=True)
            git('init', '-q')
            (Path(directory) / 'a.txt').write_text('one secret-content\n')
            git('add', '.')
            git('commit', '-qm', 'x')
            (Path(directory) / 'a.txt').write_text('two secret-content\n')
            with patch.object(channel, 'ROOT', Path(directory)):
                names = channel.dirty_tracked_paths()
        self.assertEqual(names, ['a.txt'])


class Installer(unittest.TestCase):
    def install(self, directory, forge, existing_ignore=None, tracked_pyc=False):
        root = Path(directory)
        (root / 'flake.nix').touch()
        if existing_ignore is not None:
            (root / '.gitignore').write_text(existing_ignore)
        if tracked_pyc:
            (root / 'ci/__pycache__').mkdir(parents=True)
            (root / 'ci/__pycache__/release-channel.cpython-314.pyc').write_bytes(b'x')
        policy = root / 'policy.json'
        policy.write_text(json.dumps({'targets': {'x86_64-linux': []}}))
        args = SimpleNamespace(directory=root, forge=forge, repository='fixture/tool', branch='main', policy=policy)
        yaml = SimpleNamespace(SafeDumper=SimpleNamespace(add_representer=lambda *a: None),
                               safe_dump=lambda value, **kw: json.dumps(value))
        with patch.dict('sys.modules', {'yaml': yaml}):
            installer.install(args)
        folder = root / ('.github/workflows' if forge == 'github' else '.forgejo/workflows')
        return {path.name: json.loads(path.read_text().split('\n', 1)[1]) for path in folder.glob('*.yml')}

    def test_every_job_disables_bytecode_and_bounds_forgejo_downloads(self):
        for forge in ['forgejo', 'github']:
            with tempfile.TemporaryDirectory() as directory:
                workflows = self.install(directory, forge)
            for name, workflow in workflows.items():
                for job_name, job in workflow['jobs'].items():
                    self.assertEqual(job['env']['PYTHONDONTWRITEBYTECODE'], '1', (forge, name, job_name))
                    if forge == 'forgejo':
                        for setting in ['connect-timeout = 20', 'stalled-download-timeout = 120', 'download-attempts = 5']:
                            self.assertIn(setting, job['env']['NIX_CONFIG'])

    def test_required_status_check_names_are_stable(self):
        # Branch protection requires "PR automation checks / test (pull_request)"
        # (and "Automation checks / test" for repositories without release targets).
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.install(directory, 'forgejo')
        pr = workflows['pr-checks.yml']
        self.assertEqual((pr['name'], list(pr['jobs']), pr['on']), ('PR automation checks', ['test'], {'pull_request': {}}))
        self.assertNotIn('if', pr['jobs']['test'])
        self.assertNotIn('release-channels.yml', {n for n, w in workflows.items() if 'pull_request' in w['on']})

    def test_tracked_bytecode_is_removed_and_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            self.install(directory, 'forgejo', existing_ignore='result\n', tracked_pyc=True)
            root = Path(directory)
            self.assertFalse((root / 'ci/__pycache__').exists())
            self.assertEqual((root / '.gitignore').read_text().splitlines(), ['result', '__pycache__/', '*.pyc'])

    def test_existing_ignore_rules_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            self.install(directory, 'forgejo', existing_ignore='__pycache__/\n*.pyc\n')
            self.assertEqual((Path(directory) / '.gitignore').read_text(), '__pycache__/\n*.pyc\n')


if __name__ == '__main__':
    unittest.main()
