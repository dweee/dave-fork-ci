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
        self.assertIn("CACHE['nix_config']()", source)
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

    def test_policy_only_sync_supports_forgejo_and_step_overrides_without_credential_changes(self):
        sync = runpy.run_path(str(ROOT/'ci/sync-cache-workflows.py'))
        document = {'jobs': {'build': {'runs-on': 'nix', 'steps': [
            {'env': {'NIX_CONFIG': 'cores = 3\nsubstituters = https://cache.nixos.org/\n',
                     'NIKS3_READ_NETRC': '${{ secrets.NIKS3_READ_NETRC }}'},
             'run': 'nix-build ./default.nix'}]}}}
        after, _ = sync['apply'](document)
        for config in [after['jobs']['build']['env']['NIX_CONFIG'],
                       after['jobs']['build']['steps'][0]['env']['NIX_CONFIG']]:
            for url in CACHE['PUBLIC']:
                self.assertIn(url, config)
        self.assertEqual(after['jobs']['build']['steps'][0]['env']['NIKS3_READ_NETRC'],
                         '${{ secrets.NIKS3_READ_NETRC }}')
        self.assertNotIn('permissions', after['jobs']['build'])
        self.assertEqual(sync['apply'](after)[0], after)

    def test_managed_daemon_keeps_private_reader_without_credential_forwarding(self):
        sync = runpy.run_path(str(ROOT/'ci/sync-cache-workflows.py'))
        document = {'env': {'CI_NIX_DAEMON_MANAGED': '1'}, 'jobs': {
            'build': {'steps': [{'run': 'nix build .#package'}]}}}
        after, _ = sync['apply'](document)
        config = after['jobs']['build']['env']['NIX_CONFIG']
        self.assertIn('extra-substituters = https://niks3.monke.fail\n', config)
        self.assertNotIn('netrc-file', config)
        self.assertEqual(sync['apply'](after)[0], after)

    def test_unrelated_expression_and_dynamic_cache_manual_review(self):
        sync = runpy.run_path(str(ROOT/'ci/sync-cache-workflows.py'))
        document = {'jobs': {'build': {'env': {'NIX_CONFIG': "cores = ${{ matrix.cores }}\n"},
                                      'steps': [{'run': 'nix build .#foo'}]},
                             'dynamic': {'steps': [{'uses': 'DeterminateSystems/determinate-nix-action@x',
                                                   'with': {'extra-conf': '${{ steps.conf.outputs.conf }}'}}]}}}
        after, manual = sync['apply'](document)
        self.assertIn('cores = ${{ matrix.cores }}', after['jobs']['build']['env']['NIX_CONFIG'])
        self.assertEqual(after['jobs']['dynamic'], document['jobs']['dynamic'])
        self.assertIn('dynamic: extra-conf: dynamic cache configuration', manual)

    def test_installer_policy_does_not_duplicate_job_environment(self):
        sync = runpy.run_path(str(ROOT/'ci/sync-cache-workflows.py'))
        after, _ = sync['apply']({'jobs': {'build': {'steps': [
            {'uses': 'DeterminateSystems/determinate-nix-action@x'}]}}})
        self.assertNotIn('env', after['jobs']['build'])

    @unittest.skipUnless(__import__('shutil').which('nix'), 'Nix required')
    def test_existing_user_reader_survives_appended_job_policy(self):
        import os, subprocess
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory)/'reader.conf'
            conf.write_text('extra-substituters = https://niks3.monke.fail\n')
            env = dict(os.environ, NIX_USER_CONF_FILES=str(conf), NIX_CONFIG=CACHE['nix_config']())
            result = subprocess.run(['nix', 'config', 'show', 'substituters'], env=env,
                                    check=True, capture_output=True, text=True)
            self.assertIn('https://niks3.monke.fail', result.stdout.split())
            for url in CACHE['PUBLIC']:
                self.assertIn(url, result.stdout.split())

    def test_installer_job_masks_only_inherited_cache_override(self):
        sync = runpy.run_path(str(ROOT/'ci/sync-cache-workflows.py'))
        doc = {'env': {'NIX_CONFIG': 'substituters = https://old.example\n'},
               'jobs': {'build': {'steps': [{'uses': 'DeterminateSystems/determinate-nix-action@x'}]}}}
        after, _ = sync['apply'](doc)
        self.assertEqual(after['jobs']['build']['env']['NIX_CONFIG'].strip(), '')
        self.assertEqual(sync['apply'](after)[0], after)

    def test_legacy_attic_reader_gets_public_policy_and_migration_note(self):
        sync = runpy.run_path(str(ROOT/'ci/sync-cache-workflows.py'))
        doc = {'jobs': {'build': {'steps': [{'run': 'python3 ci/configure-attic-read.py'},
                                           {'run': 'nix build .#foo'}]}}}
        after, manual = sync['apply'](doc)
        for url in CACHE['PUBLIC']:
            self.assertIn(url, after['jobs']['build']['env']['NIX_CONFIG'])
        self.assertEqual(after['jobs']['build']['steps'], doc['jobs']['build']['steps'])
        self.assertNotIn('attic', after['jobs']['build']['env']['NIX_CONFIG'])
        self.assertEqual(sync['apply'](after)[0], after)
        self.assertIn('legacy Attic reader', manual[0])
