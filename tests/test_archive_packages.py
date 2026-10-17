import importlib.util
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
