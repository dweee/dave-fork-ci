import importlib.util
import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('archive', Path(__file__).resolve().parents[1] / 'ci/archive-packages.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArchiveTests(unittest.TestCase):
    def test_old_run_backup_does_not_request_workflow_write_authority(self):
        run = {'head_sha': 'a' * 40, 'id': 42, 'html_url': 'https://example.test/runs/42'}
        with patch.object(archive, 'gh') as call:
            archive.create_release('example/fork', 'main', run, 'packages-42',
                                   [{'name': 'nix-aarch64-darwin-package-' + run['head_sha']}])
        args = call.call_args.args
        self.assertEqual(args[args.index('--target') + 1], 'main')
        notes = args[args.index('--notes') + 1]
        self.assertIn(run['head_sha'], notes)
        self.assertIn(run['html_url'], notes)
        self.assertIn('not the built source revision', notes)

    def test_upstream_candidate_revision_is_distinct_from_workflow_trigger(self):
        run = {'head_sha': 'a' * 40, 'id': 42, 'html_url': 'https://example.test/runs/42'}
        candidate = 'b' * 40
        with patch.object(archive, 'gh') as call:
            archive.create_release('example/fork', 'main', run, 'packages-42',
                                   [{'name': 'fork-cache-aarch64-darwin-' + candidate}])
        args = call.call_args.args
        notes = args[args.index('--notes') + 1]
        self.assertIn('Built source revisions: ' + candidate, notes)
        self.assertIn('Workflow trigger revision: ' + run['head_sha'], notes)

    def test_untrusted_run_cannot_publish_release_assets(self):
        good = {'repository': {'full_name': 'example/fork'}, 'head_repository': {'full_name': 'example/fork'},
                'event': 'push', 'head_branch': 'main', 'head_sha': 'a' * 40}
        archive.validate_run(good, 'example/fork', 'main')
        for key, value in [('event', 'pull_request'), ('head_branch', 'contributor'),
                           ('head_repository', {'full_name': 'other/fork'}), ('head_sha', 'invalid')]:
            bad = good | {key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                archive.validate_run(bad, 'example/fork', 'main')

    def test_scheduled_and_dispatched_upstream_archives_require_same_repo_and_default_branch(self):
        good = {'repository': {'full_name': 'example/fork'}, 'head_repository': {'full_name': 'example/fork'},
                'head_branch': 'main', 'head_sha': 'a' * 40}
        for event in ['schedule', 'repository_dispatch']:
            archive.validate_run(good | {'event': event}, 'example/fork', 'main')
            for change in [{'head_branch': 'candidate'}, {'head_repository': {'full_name': 'other/fork'}},
                           {'repository': {'full_name': 'other/fork'}}]:
                with self.subTest(event=event, change=change), self.assertRaises(ValueError):
                    archive.validate_run(good | {'event': event} | change, 'example/fork', 'main')

    def exercise_backup(self, conclusion, bad_digest=False):
        data = b'opaque synthetic package archive'
        name = 'nix-aarch64-darwin-package-' + 'a' * 40
        run = {'repository': {'full_name': 'example/fork'},
               'head_repository': {'full_name': 'example/fork'}, 'event': 'push',
               'head_branch': 'main', 'head_sha': 'a' * 40, 'id': 42,
               'conclusion': conclusion}
        artifact = {'id': 7, 'name': name, 'expired': False, 'size_in_bytes': len(data)}
        calls = []
        def gh(*args):
            calls.append(args)
            if args == ('api', 'repos/example/fork/actions/runs/42'):
                return json.dumps(run).encode()
            if args[:3] == ('api', '--paginate', '--slurp'):
                return json.dumps([{'artifacts': [artifact]}]).encode()
            if args == ('api', 'repos/example/fork/releases/tags/packages-42'):
                checksum = '0' * 64 if bad_digest else hashlib.sha256(data).hexdigest()
                return json.dumps({'assets': [{'name': name + '.zip', 'size': len(data),
                                              'digest': 'sha256:' + checksum}]}).encode()
            return b'{}'
        def command(args, **kwargs):
            if args[1:3] == ['api', '--allow-escape-sequences']:
                kwargs['stdout'].write(data)
            return subprocess.CompletedProcess(args, 0)
        env = {'GITHUB_REPOSITORY': 'example/fork', 'DEFAULT_BRANCH': 'main', 'PACKAGE_RUN_ID': '42'}
        with patch.dict(os.environ, env, clear=True), patch.object(archive, 'gh', side_effect=gh), \
             patch.object(archive.subprocess, 'run', side_effect=command):
            if bad_digest:
                with self.assertRaises(ValueError):
                    archive.main()
            else:
                archive.main()
        return calls

    def test_verified_failed_run_archives_remain_available_for_publisher_retries(self):
        for conclusion in ['failure', 'cancelled', 'timed_out', None, 'success']:
            with self.subTest(conclusion=conclusion):
                calls = self.exercise_backup(conclusion)
                uploads = [args for args in calls if args[:2] == ('release', 'upload')]
                self.assertEqual(len(uploads), 2)  # archive and checksum are retained for every result
                deletions = [args for args in calls if 'DELETE' in args]
                self.assertEqual(len(deletions), int(conclusion == 'success'))

    def test_unverified_release_asset_never_allows_source_artifact_cleanup(self):
        calls = self.exercise_backup('success', bad_digest=True)
        self.assertFalse(any('DELETE' in args for args in calls))

    def test_large_archive_splits_round_trip_without_executing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.zip'
            data = bytes(range(256)) * 100
            path.write_bytes(data)
            restored = b''
            for part in archive.chunks(path, size=4096):
                self.assertLessEqual(part.stat().st_size, 4096)
                restored += part.read_bytes()
            self.assertEqual(restored, data)
            self.assertEqual(list(path.parent.glob('*.part-*')), [])
            self.assertEqual(path.read_bytes(), data)
