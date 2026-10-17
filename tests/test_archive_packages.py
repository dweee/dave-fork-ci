import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('archive', Path(__file__).resolve().parents[1] / 'ci/archive-packages.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArchiveTests(unittest.TestCase):
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
