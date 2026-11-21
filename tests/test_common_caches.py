"""Cache coverage across generated GitHub, Forgejo and native build paths."""
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from test_channel_workflow_refs import ChannelWorkflowRefs

ROOT = Path(__file__).resolve().parents[1]
CACHE = runpy.run_path(str(ROOT/'ci/cache-config.py'))


class CommonCaches(unittest.TestCase):
    def test_public_policy_and_private_cache_order(self):
        policy = CACHE['POLICY']
        self.assertEqual(policy['substituters'][-1], 'https://niks3.monke.fail')
        self.assertEqual(len(policy['substituters']), len(set(policy['substituters'])))
        self.assertNotIn('https://cache.lantian.pub/lantian', policy['substituters'])
        self.assertIn('https://cache.lantian.pub/lantian', policy['extra-trusted-substituters'])
        self.assertNotIn('https://niks3.monke.fail', CACHE['PUBLIC'])

    def test_every_generated_job_gets_all_public_caches_and_keys(self):
        for forge in ('github', 'forgejo'):
            with self.subTest(forge=forge), tempfile.TemporaryDirectory() as directory:
                workflows = ChannelWorkflowRefs().generated(forge, directory, ('x86_64-linux', 'aarch64-darwin', 'x86_64-darwin'))
                for workflow in workflows.values():
                    for job in workflow['jobs'].values():
                        configs = [job.get('env', {}).get('NIX_CONFIG', '')]
                        configs += [str(step.get('with', {}).get(field, '')) for step in job.get('steps', [])
                                    for field in ('extra-conf', 'nix_conf')]
                        config = '\n'.join(configs)
                        for url in CACHE['PUBLIC']:
                            self.assertIn(url, config)
                        for key in CACHE['KEYS']:
                            self.assertIn(key, config)
                self.assertEqual(json.loads((Path(directory)/'ci/caches.json').read_text()), CACHE['POLICY'])

    def test_build_flags_and_test_environment_use_common_public_policy(self):
        source = (ROOT/'bin/build.py').read_text()
        self.assertIn("' '.join(CACHE['PUBLIC'])", source)
        self.assertIn("CACHE['nix_config']()", source)

    def test_reusable_workflow_configures_each_nix_install(self):
        source = (ROOT/'.github/workflows/upstream-sync.yml').read_text()
        self.assertEqual(source.count('Configure common public caches'), source.count('source-tag: v3.24.1'))
        self.assertNotIn('--option substituters https://cache.nixos.org', source)

    def test_native_install_without_extra_conf_gets_full_policy(self):
        migration = runpy.run_path(str(ROOT/'ci/migrate-niks3-workflows.py'))
        document = {'jobs': {'build': {'runs-on': 'macos-latest', 'steps': [
            {'uses': 'DeterminateSystems/determinate-nix-action@fixture'}]}}}
        after, _ = migration['migrate_document'](document)
        config = after['jobs']['build']['steps'][0]['with']['extra-conf']
        for url in CACHE['PUBLIC']:
            self.assertIn(url, config)
        self.assertEqual(migration['migrate_document'](after)[0], after)

    def test_native_nix_command_gets_policy_without_install_action(self):
        migration = runpy.run_path(str(ROOT/'ci/migrate-niks3-workflows.py'))
        document = {'env': {'NIX_CONFIG': 'cores = 2\nsubstituters = https://cache.nixos.org/\n'},
                    'jobs': {'build': {'runs-on': 'nix', 'steps': [{'run': 'nix build .#package'}]}}}
        after, _ = migration['migrate_document'](document)
        config = after['jobs']['build']['env']['NIX_CONFIG']
        self.assertIn('cores = 2\n', config)
        for url in CACHE['PUBLIC']:
            self.assertIn(url, config)
        self.assertEqual(migration['migrate_document'](after)[0], after)
