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
            'test': ('nix-light', None),
            'build_x86_64_linux': ('nix', INTERNAL),
            'build_aarch64_linux': ('braco', None),
            'build_aarch64_darwin': ('darwin-aarch64', None),
            'retain_x86_64_linux': ('nix', INTERNAL),
            'retain_aarch64_linux': ('braco', None),
            'retain_aarch64_darwin': ('nix', INTERNAL),
            'promote': ('nix', None),
        })

    def test_the_runner_network_url_reaches_only_container_jobs_on_the_nix_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.install(directory, 'forgejo')
        for name, workflow in workflows.items():
            for job_name, job in workflow['jobs'].items():
                server = job.get('env', {}).get('ATTIC_SERVER')
                if server == INTERNAL:
                    self.assertEqual(job['runs-on'], 'nix', (name, job_name))
                    self.assertIn('container', job)
                else:
                    # braco can reach the tailnet listener only after its runner
                    # unit allows it; until then it keeps the public URL.
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

    def test_aarch64_retention_runs_on_braco_like_its_build_job(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = self.install(directory, 'forgejo')['release-channels.yml']['jobs']
        retain, build = jobs['retain_aarch64_linux'], jobs['build_aarch64_linux']
        self.assertEqual((retain['runs-on'], build['runs-on']), ('braco', 'braco'))
        # braco is a host-mode runner: no container, no Node-based actions, and the
        # public cache URL (no ATTIC_SERVER) until its runner may reach Attic privately.
        for job in (retain, build):
            self.assertNotIn('container', job)
            self.assertNotIn('ATTIC_SERVER', job['env'])
            self.assertTrue(all('uses' not in step for step in job['steps']))
        self.assertEqual(retain['env'], build['env'])
        self.assertEqual(retain['timeout-minutes'], build['timeout-minutes'])
        # Same checkout and Nix-based Python invocation as the working braco build job.
        self.assertEqual(retain['steps'][0], build['steps'][0] | {'env': retain['steps'][0]['env']})
        self.assertEqual(retain['steps'][1]['run'], build['steps'][1]['run'])
        retention = next(step for step in retain['steps'] if 'release-channel.py retain' in step.get('run', ''))
        self.assertTrue(retention['run'].startswith('nix shell '))
        self.assertTrue(retention['run'].endswith('ci/release-channel.py retain --system aarch64-linux'))
        # Scheduled or owner-dispatched only, like every other retention job.
        self.assertIn("github.event_name == 'schedule'", retain['if'])

    def test_only_aarch64_linux_retention_moves_to_braco(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = self.install(directory, 'forgejo')['release-channels.yml']['jobs']
        self.assertEqual({name for name, job in jobs.items() if job['runs-on'] == 'braco' and name.startswith('retain_')},
                         {'retain_aarch64_linux'})

    def test_github_retention_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = self.install(directory, 'github')['release-channels.yml']['jobs']
        self.assertEqual({job['runs-on'] for name, job in jobs.items() if name.startswith('retain_')}, {'ubuntu-24.04'})

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
