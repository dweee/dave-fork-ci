"""Regress the bare schedule ref observed on Forgejo, without a YAML dependency."""
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


def matches(expression, event, ref):
    expression = expression.removeprefix('${{ ').removesuffix(' }}')
    expression = expression.replace('github.event_name', repr(event)).replace('github.ref', repr(ref))
    return eval(expression.replace('&&', ' and ').replace('||', ' or '),
                {'__builtins__': {}, 'format': lambda pattern, value: pattern.format(value)})


class ChannelWorkflowRefs(unittest.TestCase):
    def generated(self, forge, directory, systems=('x86_64-linux',), extra=None):
        root = Path(directory)
        (root / 'flake.nix').touch()
        policy = root / 'policy.json'
        policy.write_text(json.dumps({'targets': {system: [] for system in systems}, **(extra or {})}))
        args = SimpleNamespace(directory=root, forge=forge, repository='fixture/tool', branch='main', policy=policy)
        # JSON is a YAML subset. Capture generated job definitions using only
        # stdlib; production serialization remains PyYAML's safe_dump.
        yaml = SimpleNamespace(SafeDumper=SimpleNamespace(add_representer=lambda *a: None),
                               safe_dump=lambda value, **kw: json.dumps(value))
        with patch.dict('sys.modules', {'yaml': yaml}):
            installer.install(args)
        folder = root / ('.github/workflows' if forge == 'github' else '.forgejo/workflows')
        names = ['release-channels', 'scheduled-updates', 'pr-checks']
        return {name: json.loads((folder / (name + '.yml')).read_text().split('\n', 1)[1])
                for name in names if (folder / (name + '.yml')).exists()}

    def test_forgejo_opt_in_only_changes_selected_container_labels(self):
        image = 'sha256:' + 'a' * 64
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.generated('forgejo', directory, ('x86_64-linux', 'aarch64-linux'),
                                       {'forgejoDeterminateImage': image, 'forgejoDeterminateLabels': ['nix']})
        changed = 0
        for workflow in workflows.values():
            for job in workflow['jobs'].values():
                steps = [step for step in job['steps'] if step.get('name') == 'Start isolated Determinate daemon']
                if job['runs-on'] == 'nix':
                    changed += 1
                    self.assertEqual(job['container']['image'], image)
                    self.assertEqual(job['env']['NIX_REMOTE'], 'daemon')
                    self.assertIn('eval-cores = 2', job['env']['NIX_CONFIG'])
                    self.assertEqual(len(steps), 1)
                    self.assertIn('env -u NIX_REMOTE nix-daemon', steps[0]['run'])
                else:
                    self.assertFalse(steps)
        self.assertGreater(changed, 0)

    def test_github_bootstraps_determinate_with_intel_macos_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.generated('github', directory, ('x86_64-linux', 'x86_64-darwin'))
        for workflow in workflows.values():
            for job in workflow['jobs'].values():
                steps = job['steps']
                installs = [step for step in steps if step.get('uses') == installer.INSTALL]
                if not installs:
                    continue
                self.assertEqual(len(installs), 1)
                step = installs[0]
                self.assertEqual(step['with']['source-tag'], 'v3.24.1')
                self.assertIn('parallel-eval', step['with']['extra-conf'])
                self.assertIn('eval-cores = 2', step['with']['extra-conf'])
                self.assertIn("runner.arch == 'X64'", step['if'])
                fallback = [s for s in steps if s.get('uses') == installer.INTEL_DARWIN_INSTALL]
                self.assertEqual(len(fallback), 1)
                self.assertIn("runner.os == 'macOS'", fallback[0]['if'])
                self.assertTrue(any(s.get('name') == 'Check Determinate Nix and daemon' for s in steps))

    def test_pr_selection_does_not_reference_dispatch_inputs(self):
        for forge in ('github', 'forgejo'):
            with tempfile.TemporaryDirectory() as directory:
                workflows = self.generated(forge, directory)
            pr = workflows['pr-checks']['jobs']['test']['steps'][-1]['env']
            release = workflows['release-channels']['jobs']['test']['steps'][-1]['env']
            self.assertEqual(pr, {'REVISION': '${{ github.sha }}'})
            self.assertEqual(release, {'REVISION': '${{ inputs.revision || github.sha }}'})

    def test_forgejo_cron_runs_monitor_inputs_build_and_retention_on_bare_default_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.generated('forgejo', directory)
        for workflow in workflows.values():
            for name, job in workflow['jobs'].items():
                if 'if' in job:
                    with self.subTest(job=name):
                        self.assertTrue(matches(job['if'], 'schedule', 'main'))
                        self.assertFalse(matches(job['if'], 'schedule', 'feature'))
                        self.assertFalse(matches(job['if'], 'pull_request', 'refs/pull/3/head'))
                        self.assertFalse(matches(job['if'], 'push', 'refs/tags/main'))
                        self.assertFalse(matches(job['if'], 'workflow_dispatch', 'main'))

    def test_fully_qualified_dispatch_ref_still_runs_both_forges(self):
        for forge in ['github', 'forgejo']:
            with tempfile.TemporaryDirectory() as directory:
                workflows = self.generated(forge, directory)
            for workflow in workflows.values():
                for name, job in workflow['jobs'].items():
                    if 'if' in job:
                        with self.subTest(forge=forge, job=name):
                            self.assertTrue(matches(job['if'], 'workflow_dispatch', 'refs/heads/main'))

    def test_github_does_not_accept_bare_schedule_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = self.generated('github', directory)
        for workflow in workflows.values():
            for name, job in workflow['jobs'].items():
                if 'if' in job:
                    with self.subTest(job=name):
                        self.assertFalse(matches(job['if'], 'schedule', 'main'))
                        self.assertTrue(matches(job['if'], 'schedule', 'refs/heads/main'))

    def test_pr_regressions_do_not_share_or_replace_pending_trusted_releases(self):
        for forge in ['github', 'forgejo']:
            with tempfile.TemporaryDirectory() as directory:
                workflows = self.generated(forge, directory)
            release = workflows['release-channels']
            pr_checks = workflows['pr-checks']
            # Release runs are trusted push/schedule/dispatch only. PRs run a
            # separate test-only workflow, so Forgejo never sees a release run
            # whose build/retain/promote jobs are all skipped.
            self.assertNotIn('pull_request', release['on'])
            self.assertIn('pull_request', pr_checks['on'])
            self.assertEqual(set(pr_checks['jobs']), {'test'})
            concurrency = release['concurrency']
            self.assertEqual(concurrency['group'], 'verified-release-channels')
            self.assertFalse(concurrency['cancel-in-progress'])

    def test_cache_audits_run_where_they_can_read_and_builds_remain_native(self):
        platforms = {
            'github': {'x86_64-linux': 'ubuntu-24.04', 'aarch64-linux': 'ubuntu-24.04-arm',
                       'aarch64-darwin': 'macos-15'},
            'forgejo': {'x86_64-linux': 'nix', 'aarch64-linux': 'braco'},
        }
        for forge, native_runners in platforms.items():
            with tempfile.TemporaryDirectory() as directory:
                jobs = self.generated(forge, directory, native_runners)['release-channels']['jobs']
            for system, native_runner in native_runners.items():
                suffix = system.replace('-', '_')
                with self.subTest(forge=forge, system=system):
                    self.assertEqual(jobs['build_' + suffix]['runs-on'], native_runner)
                    retention = jobs['retain_' + suffix]
                    # Forgejo audits run on the general icicle runner, except aarch64
                    # Linux, which evaluates and reads natively on the host-mode braco.
                    expected = ('ubuntu-24.04' if forge == 'github'
                                else 'braco' if system == 'aarch64-linux' else 'nix')
                    self.assertEqual(retention['runs-on'], expected)
                    self.assertTrue(any('retain --system ' + system in step.get('run', '')
                                        for step in retention['steps']))
                    if forge == 'forgejo':
                        self.assertEqual('container' in retention, expected == 'nix')


if __name__ == '__main__':
    unittest.main()
