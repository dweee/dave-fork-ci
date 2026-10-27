"""Per-repository schedule offsets: stable, spread, and cadence-preserving."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('channel_installer', ROOT / 'ci/install-channel-automation.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

REPOSITORIES = ['d/' + name for name in [
    'sable', 'cursor-archive', 'music-library-analyzer', 'music-library-tui', 'restic-dash', 'nw-tracker',
    'music-sorterer', 'vacwatch', 'mc-sandbox', 'subnetter', 'esttoomid-lastfm', 'neversnooze-scanner',
    'apfs-space-lock', 'fleet-sentry']]


class Schedule(unittest.TestCase):
    def test_known_offsets_are_pinned(self):
        # A change here moves a repository's schedule; review it deliberately.
        self.assertEqual(installer.release_cron('d/sable'), '13 5 * * *')
        self.assertEqual(installer.updates_cron('d/sable'), '15 1,7,13,19 * * *')
        self.assertEqual(installer.release_cron('d/fleet-sentry'), '3 3 * * *')
        self.assertEqual(installer.updates_cron('d/fleet-sentry'), '53 3,9,15,21 * * *')

    def test_offsets_do_not_depend_on_the_hash_seed(self):
        code = ('import importlib.util,sys;'
                's=importlib.util.spec_from_file_location("i",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'print(m.release_cron("d/sable"),m.updates_cron("d/sable"))')
        results = {subprocess.run([sys.executable, '-c', code, str(ROOT / 'ci/install-channel-automation.py')],
                                  env={'PYTHONHASHSEED': seed, 'PYTHONDONTWRITEBYTECODE': '1'},
                                  capture_output=True, text=True, check=True).stdout
                   for seed in ['1', '2', 'random']}
        self.assertEqual(len(results), 1)

    def test_release_runs_nightly_between_02_and_06_utc(self):
        for repository in REPOSITORIES + ['fixture/' + str(number) for number in range(500)]:
            minute, hour, *rest = installer.release_cron(repository).split()
            self.assertEqual(rest, ['*', '*', '*'])
            self.assertIn(int(hour), range(2, 7))
            self.assertIn(int(minute), range(60))

    def test_updates_keep_a_six_hour_cadence(self):
        for repository in REPOSITORIES + ['fixture/' + str(number) for number in range(500)]:
            minute, hours, *rest = installer.updates_cron(repository).split()
            self.assertEqual(rest, ['*', '*', '*'])
            hours = [int(hour) for hour in hours.split(',')]
            self.assertEqual(len(hours), 4)
            self.assertEqual([b - a for a, b in zip(hours, hours[1:])], [6, 6, 6])
            self.assertIn(hours[0], range(6))
            self.assertIn(int(minute), range(60))

    def test_repositories_are_spread_out(self):
        releases = {installer.release_cron(repository) for repository in REPOSITORIES}
        updates = {installer.updates_cron(repository) for repository in REPOSITORIES}
        self.assertGreaterEqual(len(releases), len(REPOSITORIES) - 1)
        self.assertGreaterEqual(len(updates), len(REPOSITORIES) - 1)
        sample = {installer.release_cron('fixture/' + str(number)) for number in range(1000)}
        self.assertGreater(len(sample), 200)
        self.assertEqual({int(cron.split()[1]) for cron in sample}, {2, 3, 4, 5, 6})

    def test_workflows_use_the_derived_schedule_in_both_forges(self):
        for forge in ['forgejo', 'github']:
            with self.subTest(forge=forge), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'flake.nix').touch()
                policy = root / 'policy.json'
                policy.write_text(json.dumps({'targets': {'x86_64-linux': []}}))
                args = SimpleNamespace(directory=root, forge=forge, repository='d/sable', branch='main', policy=policy)
                yaml = SimpleNamespace(SafeDumper=SimpleNamespace(add_representer=lambda *a: None),
                                       safe_dump=lambda value, **kw: json.dumps(value))
                with patch.dict('sys.modules', {'yaml': yaml}):
                    installer.install(args)
                folder = root / ('.github/workflows' if forge == 'github' else '.forgejo/workflows')
                load = lambda name: json.loads((folder / name).read_text().split('\n', 1)[1])
                self.assertEqual(load('release-channels.yml')['on']['schedule'], [{'cron': '13 5 * * *'}])
                self.assertEqual(load('scheduled-updates.yml')['on']['schedule'], [{'cron': '15 1,7,13,19 * * *'}])
                self.assertIn('SHA-256', (root / 'ci/CHANNELS.md').read_text())


if __name__ == '__main__':
    unittest.main()
