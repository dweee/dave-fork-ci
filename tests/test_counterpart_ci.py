"""githubCounterpartCi: skip Forgejo duplicates of a GitHub counterpart's CI, without a YAML dependency."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('channel_installer', ROOT / 'ci/install-channel-automation.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

IGNORED = ['.forgejo/**', '.github/**', 'docs/**', '**/*.md', 'LICENSE*', 'COPYING*', 'DCO']


class CounterpartCi(unittest.TestCase):
    def install(self, directory, forge='forgejo', **policy):
        root = Path(directory)
        (root / 'flake.nix').touch()
        policy_file = root / 'policy.json'
        policy_file.write_text(json.dumps({'targets': {'x86_64-linux': []}, **policy}))
        args = SimpleNamespace(directory=root, forge=forge, repository='fixture/tool', branch='main', policy=policy_file)
        yaml = SimpleNamespace(SafeDumper=SimpleNamespace(add_representer=lambda *a: None),
                               safe_dump=lambda value, **kw: json.dumps(value))
        with patch.dict('sys.modules', {'yaml': yaml}):
            installer.install(args)
        return root

    def read(self, root, name):
        folder = root / '.forgejo/workflows'
        return json.loads((folder / name).read_text().split('\n', 1)[1])

    def snapshot(self, root):
        return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob('*'))
                if p.is_file() and p.name != 'policy.json' and p.name != 'channels.json'}

    def test_default_output_is_unchanged(self):
        with tempfile.TemporaryDirectory() as absent, tempfile.TemporaryDirectory() as disabled:
            absent_root = self.install(absent)
            disabled_root = self.install(disabled, githubCounterpartCi=False)
            self.assertEqual(self.snapshot(absent_root), self.snapshot(disabled_root))
            triggers = self.read(absent_root, 'release-channels.yml')['on']
            self.assertEqual(list(triggers), ['push', 'pull_request', 'schedule', 'workflow_dispatch'])
            self.assertEqual(triggers['push'], {'branches': ['main']})
            self.assertNotIn('githubCounterpartCi', (absent_root / 'ci/CHANNELS.md').read_text())

    def test_default_leaves_an_existing_nix_workflow_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / '.forgejo/workflows'
            folder.mkdir(parents=True)
            (folder / 'nix.yml').write_text('name: Nix packages\n')
            self.install(directory)
            self.assertEqual((folder / 'nix.yml').read_text(), 'name: Nix packages\n')

    def test_option_trims_release_triggers_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as plain, tempfile.TemporaryDirectory() as trimmed:
            default = self.read(self.install(plain), 'release-channels.yml')
            workflow = self.read(self.install(trimmed, githubCounterpartCi=True), 'release-channels.yml')
        triggers = workflow['on']
        self.assertEqual(list(triggers), ['push', 'schedule', 'workflow_dispatch'])
        self.assertEqual(triggers['push'], {'branches': ['main'], 'paths-ignore': IGNORED})
        self.assertEqual(triggers['schedule'], [{'cron': '43 3 * * *'}])
        self.assertEqual(triggers['workflow_dispatch'], default['on']['workflow_dispatch'])
        self.assertEqual(triggers['workflow_dispatch']['inputs']['channel']['default'], 'tested')
        for key in ('name', 'permissions', 'concurrency', 'jobs'):
            self.assertEqual(workflow[key], default[key])

    def test_option_never_ignores_build_or_automation_inputs(self):
        for path in ('nix/package.nix', 'flake.nix', 'flake.lock', 'ci/channels.json', 'ci/release-channel.py',
                     'src/main.ts', 'README.txt'):
            self.assertFalse(any(self.glob_matches(pattern, path) for pattern in IGNORED), path)
        for path in ('.forgejo/workflows/nix.yml', '.github/workflows/build.yml', 'docs/a/b.html',
                     'README.md', 'sub/dir/CHANGELOG.md', 'LICENSE', 'LICENSE-APACHE', 'DCO'):
            self.assertTrue(any(self.glob_matches(pattern, path) for pattern in IGNORED), path)

    @staticmethod
    def glob_matches(pattern, path):
        import re
        regex = re.escape(pattern).replace(r'\*\*/', '(?:.*/)?').replace(r'\*\*', '.*').replace(r'\*', '[^/]*')
        return re.fullmatch(regex, path) is not None

    def test_option_removes_a_stale_nix_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / '.forgejo/workflows'
            folder.mkdir(parents=True)
            (folder / 'nix.yml').write_text('name: Nix packages\n')
            (folder / 'other.yml').write_text('name: Other\n')
            self.install(directory, githubCounterpartCi=True)
            self.assertFalse((folder / 'nix.yml').exists())
            self.assertTrue((folder / 'other.yml').exists())
            self.assertTrue((folder / 'release-channels.yml').exists())
            self.assertIn('githubCounterpartCi', (Path(directory) / 'ci/CHANNELS.md').read_text())
            # A repeat run is a no-op rather than an error.
            self.install(directory, githubCounterpartCi=True)
            self.assertFalse((folder / 'nix.yml').exists())

    def test_option_is_recorded_in_the_installed_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.install(directory, githubCounterpartCi=True)
            self.assertIs(json.loads((root / 'ci/channels.json').read_text())['githubCounterpartCi'], True)

    def test_option_is_rejected_for_github_and_non_boolean_values(self):
        for forge, value in (('github', True), ('forgejo', 'yes'), ('forgejo', 1)):
            with self.subTest(forge=forge, value=value), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(SystemExit):
                    self.install(directory, forge=forge, githubCounterpartCi=value)


if __name__ == '__main__':
    unittest.main()
