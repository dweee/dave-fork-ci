"""Cache reads use the runner-network Attic URL only on the icicle `nix` runner."""
import hashlib
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

INTERNAL = 'http://172.29.0.1:8080'
TAILNET = 'http://icicle.eel-ling.ts.net:8080'
SYSTEMS = ['x86_64-linux', 'aarch64-linux', 'aarch64-darwin']


class InternalAtticUrl(unittest.TestCase):
    def install(self, directory, forge):
        root = Path(directory)
        (root / 'flake.nix').touch()
        policy = root / 'policy.json'
        policy.write_text(json.dumps({'targets': {system: [] for system in SYSTEMS}}))
        args = SimpleNamespace(directory=root, forge=forge, repository='fixture/tool', branch='main', policy=policy)
        yaml = SimpleNamespace(SafeDumper=SimpleNamespace(add_representer=lambda *a: None),
                               safe_dump=lambda value, **kw: json.dumps(value))
        with patch.dict('sys.modules', {'yaml': yaml}):
            installer.install(args)
        folder = root / ('.github/workflows' if forge == 'github' else '.forgejo/workflows')
        return {path.name: json.loads(path.read_text().split('\n', 1)[1]) for path in folder.glob('*.yml')}

    def test_forgejo_jobs_on_the_nix_runner_that_read_the_cache_use_the_internal_url(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = self.install(directory, 'forgejo')['release-channels.yml']['jobs']
        table = {name: (job['runs-on'], job.get('env', {}).get('ATTIC_SERVER')) for name, job in jobs.items()}
        self.assertEqual(table, {
            'test': ('nix', None),
            'build_x86_64_linux': ('nix', INTERNAL),
            'build_aarch64_linux': ('braco', TAILNET),
            'build_aarch64_darwin': ('darwin-aarch64', None),
            'retain_x86_64_linux': ('nix', INTERNAL),
            'retain_aarch64_linux': ('nix', INTERNAL),
            'retain_aarch64_darwin': ('nix', INTERNAL),
            'promote': ('nix', None),
        })

    def test_each_url_reaches_only_the_runner_that_can_route_to_it(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.install(directory, 'forgejo')
        for name, workflow in workflows.items():
            for job_name, job in workflow['jobs'].items():
                server = job.get('env', {}).get('ATTIC_SERVER')
                if server == INTERNAL:
                    self.assertEqual(job['runs-on'], 'nix', (name, job_name))
                    self.assertIn('container', job)
                elif server == TAILNET:
                    self.assertEqual(job['runs-on'], 'braco', (name, job_name))
                    self.assertNotIn('container', job)
                else:
                    self.assertIsNone(server, (name, job_name))
        # The shared NIX_CONFIG stays identical, so no job gains a cache or key.
        configs = {json.dumps(job['env']['NIX_CONFIG']) for w in workflows.values() for job in w['jobs'].values()}
        self.assertEqual(len(configs), 1)

    def test_only_cache_reading_jobs_carry_the_variable(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.install(directory, 'forgejo')
        for name in ['scheduled-updates.yml', 'pr-checks.yml']:
            self.assertNotIn(INTERNAL, json.dumps(workflows[name]))
            self.assertNotIn(TAILNET, json.dumps(workflows[name]))

    def test_github_workflows_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.install(directory, 'github')
        self.assertNotIn('172.29', json.dumps(workflows))
        self.assertNotIn('ts.net', json.dumps(workflows))
        self.assertNotIn('ATTIC_SERVER', json.dumps(workflows))

    def test_installed_helpers_match_the_recorded_hashes(self):
        recorded = json.loads((ROOT / 'ci/channel-tool-hashes.json').read_text())
        self.assertEqual(recorded, {name: hashlib.sha256((ROOT / 'ci' / name).read_bytes()).hexdigest()
                                    for name in installer.HELPERS})
        with tempfile.TemporaryDirectory() as directory:
            self.install(directory, 'forgejo')
            installed = json.loads((Path(directory) / 'ci/channel-tool-hashes.json').read_text())
        self.assertEqual(installed, recorded)

    def test_the_installer_and_helpers_agree_on_the_internal_origin(self):
        self.assertEqual(installer.INTERNAL_ATTIC, INTERNAL)
        self.assertEqual(installer.TAILNET_ATTIC, TAILNET)
        for name in ['release-channel.py', 'configure-attic-read.py']:
            text = (ROOT / 'ci' / name).read_text()
            self.assertIn(INTERNAL, text)
            self.assertIn(TAILNET, text)


if __name__ == '__main__':
    unittest.main()
