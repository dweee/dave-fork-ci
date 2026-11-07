"""Native workflow migration must preserve gates and reject ambiguous scripts."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('migrate_niks3', ROOT / 'ci/migrate-niks3-workflows.py')
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)
HAS_YAML = importlib.util.find_spec('yaml') is not None
READER_GUARD = "${{ (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}"
PUBLISH_GUARD = "${{ vars.ATTIC_PUSH_ENABLED == 'true' && needs.build.result == 'success' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}"
PR_GUARD = "${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository && github.actor == github.repository_owner && vars.ATTIC_APPROVED_PR_SHA != '' && github.event.pull_request.head.sha == vars.ATTIC_APPROVED_PR_SHA }}"
ATTIC_INSTALL = '''set -euo pipefail
client=$(nix build --inputs-from . 'nixpkgs#attic-client' --no-update-lock-file --no-write-lock-file --out-link "${RUNNER_TEMP:-/tmp}/attic-client" --print-out-paths --max-jobs 0 --builders "")
test -x "$client/bin/attic"
echo "binary=$client/bin/attic" >> "$GITHUB_OUTPUT"
'''
BUILD_RUN = '''set -euo pipefail
target=".#packages.$TARGET_SYSTEM.$TARGET_PACKAGE"
nix build "$target" --no-link --print-out-paths > outputs.txt
echo "literal vars.ATTIC_PUSH_ENABLED is documentation" >> "$GITHUB_STEP_SUMMARY"
'''


def native_workflow(dual=True, approved_pr=True, cleanup=True):
    build = [
        {'uses': 'DeterminateSystems/determinate-nix-action@' + 'a' * 40,
         'with': {'extra-conf': 'experimental-features = nix-command flakes\nextra-substituters =\nextra-trusted-public-keys = ' + migration.OLD_KEY + '\n'}},
        {'name': 'Private reader', 'if': READER_GUARD, 'env': {'ATTIC_TOKEN': '${{ secrets.ATTIC_TOKEN }}'}, 'run': 'python3 ci/configure-attic-read.py'},
    ]
    if approved_pr:
        build.append({'name': 'Reviewed PR reader', 'if': PR_GUARD,
                      'env': {'ATTIC_PULL_TOKEN': '${{ secrets.ATTIC_PULL_TOKEN }}', 'ATTIC_APPROVED_PR_SHA': '${{ vars.ATTIC_APPROVED_PR_SHA }}'},
                      'run': 'python3 ci/configure-attic-read.py --pr-read'})
    build.append({'name': 'Build', 'env': {'TARGET_SYSTEM': '${{ matrix.system }}', 'TARGET_PACKAGE': '${{ matrix.package }}'}, 'run': BUILD_RUN})
    if cleanup:
        build.append({'name': 'Clean read credential', 'if': '${{ always() }}', 'run': 'python3 ci/configure-attic-read.py --cleanup'})
    build.extend([
        {'name': 'Export', 'env': {'ATTIC_MAX_CLOSURE_MIB': "${{ vars.ATTIC_MAX_CLOSURE_MIB || '4096' }}"}, 'run': 'python3 ci/transfer-attic.py export --directory attic-transfer'},
        {'uses': 'actions/upload-artifact@' + 'b' * 40, 'with': {'name': 'output-${{ github.sha }}', 'path': 'attic-transfer/', 'compression-level': 0}},
    ])
    publish = [
        {'uses': 'actions/checkout@' + 'c' * 40, 'with': {'persist-credentials': False}},
        {'name': 'Install trusted Attic', 'id': 'attic', 'timeout-minutes': 15, 'run': ATTIC_INSTALL},
        {'uses': 'actions/download-artifact@' + 'd' * 40, 'with': {'name': 'output-${{ github.sha }}', 'path': 'attic-transfer'}},
        {'name': 'Import', 'env': {'ATTIC_MAX_CLOSURE_MIB': '4096'}, 'run': 'python3 ci/transfer-attic.py import --directory attic-transfer'},
        {'name': 'Publish Attic', 'timeout-minutes': 180,
         'env': {'ATTIC_BIN': '${{ steps.attic.outputs.binary }}', 'ATTIC_TOKEN': '${{ secrets.ATTIC_TOKEN }}', 'ATTIC_ENDPOINT': 'https://attic.monke.fail', 'ATTIC_CACHE': 'flakey'},
         'run': 'python3 ci/push-attic.py --outputs attic-transfer/outputs.txt'},
    ]
    if dual:
        publish.insert(3, {'name': 'Install pinned Niks3 publisher', 'run': 'bash ci/install-niks3.sh'})
        publish.append({'name': 'Niks3 publication', 'run': 'python3 ci/push-niks3.py --outputs attic-transfer/outputs.txt'})
    return {'name': 'Native cache', 'on': {'push': {'branches': ['flakey']}, 'pull_request': None, 'workflow_dispatch': None},
            'permissions': {'contents': 'read', 'checks': 'read'},
            'jobs': {'build': {'runs-on': 'macos-15', 'strategy': {'matrix': {'package': ['fixture']}}, 'steps': build},
                     'publish': {'if': PUBLISH_GUARD, 'needs': ['build'], 'permissions': {'contents': 'read'}, 'runs-on': 'ubuntu-24.04', 'steps': publish}}}


class MigrationSemantics(unittest.TestCase):
    def test_native_build_without_prior_reader_gets_trusted_reader_and_cleanup(self):
        before = native_workflow(approved_pr=False, cleanup=False)
        before['jobs']['build']['steps'] = [s for s in before['jobs']['build']['steps']
                                             if 'configure-attic-read.py' not in s.get('run', '')]
        after, _ = migration.migrate_document(before)
        steps = after['jobs']['build']['steps']
        reader = next(i for i, s in enumerate(steps) if s.get('run') == 'python3 ci/configure-niks3.py --oidc')
        build = next(i for i, s in enumerate(steps) if s.get('run') == BUILD_RUN)
        cleanup = next(i for i, s in enumerate(steps) if s.get('run') == 'python3 ci/configure-niks3.py --cleanup')
        export = next(i for i, s in enumerate(steps) if 'ci/transfer-attic.py export' in s.get('run', ''))
        self.assertEqual(steps[reader]['if'], READER_GUARD)
        self.assertEqual(steps[cleanup]['if'], '${{ always() }}')
        self.assertLess(reader, build)
        self.assertLess(build, cleanup)
        self.assertLess(cleanup, export)
        self.assertEqual(after['jobs']['build']['permissions']['id-token'], 'write')
        again, _ = migration.migrate_document(after)
        self.assertEqual(again, after)

    def test_removes_static_credentials_but_preserves_guards_build_and_transfers(self):
        before = native_workflow()
        unchanged = copy.deepcopy(before)
        after, manual = migration.migrate_document(before)
        self.assertEqual(before, unchanged, 'planning mutated caller input')
        self.assertEqual(manual, [])
        self.assertEqual(after['on'], before['on'])
        self.assertEqual(after['jobs']['publish']['if'], PUBLISH_GUARD.replace('vars.ATTIC_PUSH_ENABLED', migration.CACHE_FLAG))
        reader = next(s for s in after['jobs']['build']['steps'] if s.get('run') == 'python3 ci/configure-niks3.py --oidc')
        self.assertEqual(reader['if'], READER_GUARD)
        self.assertNotIn('env', reader)
        serialized = json.dumps(after)
        for obsolete in ('secrets.ATTIC_TOKEN', 'secrets.ATTIC_PULL_TOKEN', 'ATTIC_APPROVED_PR_SHA', 'ci/configure-attic-read.py', 'ci/push-attic.py', 'nixpkgs#attic-client'):
            self.assertNotIn(obsolete, serialized)
        self.assertEqual(next(s['run'] for s in after['jobs']['build']['steps'] if s.get('name') == 'Build'), BUILD_RUN)
        for job in ('build', 'publish'):
            self.assertEqual(after['jobs'][job]['needs'] if job == 'publish' else after['jobs'][job]['strategy'], before['jobs'][job]['needs'] if job == 'publish' else before['jobs'][job]['strategy'])
            for step in before['jobs'][job]['steps']:
                if 'ci/transfer-attic.py' in step.get('run', '') or 'actions/upload-artifact@' in step.get('uses', '') or 'actions/download-artifact@' in step.get('uses', ''):
                    self.assertIn(step, after['jobs'][job]['steps'])
        self.assertEqual(after['jobs']['build']['permissions'], {'contents': 'read', 'checks': 'read', 'id-token': 'write'})
        self.assertEqual(after['jobs']['publish']['permissions'], {'contents': 'read', 'id-token': 'write'})

    def test_dual_publisher_becomes_exactly_one_niks3_publication(self):
        after, _ = migration.migrate_document(native_workflow())
        runs = [s.get('run', '') for s in after['jobs']['publish']['steps']]
        self.assertEqual(sum('ci/push-niks3.py' in run for run in runs), 1)
        self.assertEqual(sum(run == 'bash ci/install-niks3.sh' for run in runs), 1)
        self.assertLess(runs.index('bash ci/install-niks3.sh'), runs.index('python3 ci/transfer-attic.py import --directory attic-transfer'))

    def test_attic_only_publisher_installs_niks3_before_import(self):
        after, _ = migration.migrate_document(native_workflow(dual=False))
        steps = after['jobs']['publish']['steps']
        runs = [s.get('run', '') for s in steps]
        self.assertIn('python3 ci/push-niks3.py --outputs attic-transfer/outputs.txt', runs)
        self.assertLess(runs.index('bash ci/install-niks3.sh'), runs.index('python3 ci/transfer-attic.py import --directory attic-transfer'))
        self.assertEqual(next(s['timeout-minutes'] for s in steps if 'ci/push-niks3.py' in s.get('run', '')), 180)

    def test_adds_missing_reader_cleanup_before_export(self):
        after, _ = migration.migrate_document(native_workflow(cleanup=False))
        steps = after['jobs']['build']['steps']
        cleanup = next(i for i, s in enumerate(steps) if s.get('run') == 'python3 ci/configure-niks3.py --cleanup')
        export = next(i for i, s in enumerate(steps) if 'ci/transfer-attic.py export' in s.get('run', ''))
        self.assertLess(cleanup, export)
        self.assertEqual(steps[cleanup]['if'], '${{ always() }}')

    def test_second_migration_has_no_semantic_changes(self):
        once, _ = migration.migrate_document(native_workflow())
        twice, _ = migration.migrate_document(once)
        self.assertEqual(once, twice)

    def test_repins_shared_controller_and_removes_obsolete_cache_inputs(self):
        document = {'on': {'schedule': [{'cron': '3 4 * * *'}]}, 'jobs': {'upstream': {
            'if': "${{ vars.GUARDED_UPSTREAM_SYNC_ENABLED == 'true' }}",
            'uses': migration.CALLER + '1' * 40,
            'permissions': {'contents': 'write', 'statuses': 'write'},
            'with': {'tool-repository': 'dweee/dave-fork-ci', 'tool-revision': '1' * 40, 'cache': False,
                     'auto-merge': "${{ vars.FORK_CI_AUTO_MERGE == 'true' && vars.ATTIC_PUSH_ENABLED == 'true' }}",
                     'attic-substituter': '${{ vars.ATTIC_SUBSTITUTER }}', 'attic-public-key': '${{ vars.ATTIC_PUBLIC_KEY }}'},
            'secrets': {'ATTIC_TOKEN': '${{ secrets.ATTIC_TOKEN }}', 'OTHER': '${{ secrets.OTHER }}'},
        }}}
        after, _ = migration.migrate_document(document, '2' * 40)
        job = after['jobs']['upstream']
        self.assertEqual(job['uses'], migration.CALLER + '2' * 40)
        self.assertEqual(job['with']['tool-revision'], '2' * 40)
        self.assertFalse(job['with']['cache'], 'disabled upstream cache gate was enabled')
        self.assertEqual(job['if'], document['jobs']['upstream']['if'])
        self.assertEqual(job['secrets'], {'OTHER': '${{ secrets.OTHER }}'})
        self.assertFalse(any(key.startswith('attic-') for key in job['with']))
        self.assertEqual(job['permissions']['statuses'], 'write')
        self.assertEqual(job['permissions']['id-token'], 'write')

    def test_old_cache_flag_fallback_does_not_override_explicit_false(self):
        after, _ = migration.migrate_document(native_workflow())
        self.assertIn("(vars.NIKS3_PUSH_ENABLED || vars.ATTIC_PUSH_ENABLED) == 'true'", after['jobs']['publish']['if'])

    def test_cache_only_custom_publisher_requires_manual_repair(self):
        document = {'jobs': {'custom': {'steps': [{'name': 'Build and publish', 'env': {'ATTIC_TOKEN': '${{ secrets.ATTIC_TOKEN }}'}, 'run': 'python3 ci/build-cache-only.py'}]}}}
        with self.assertRaisesRegex(migration.MigrationError, 'build-cache-only.py requires manual'):
            migration.migrate_document(document)
        document['jobs']['custom']['steps'][0].pop('env')
        after, manual = migration.migrate_document(document)
        self.assertEqual(after, document)
        self.assertIn('build-cache-only.py', manual[0])

    def test_mixed_reader_publisher_and_client_install_fail_closed(self):
        for command in (
            'python3 ci/configure-attic-read.py && ./extra-command',
            'python3 ci/configure-attic-read.py\npython3 ci/test-configure-attic-read.py\n',
            'python3 ci/push-attic.py --outputs outputs.txt\necho keep-this\n',
            ATTIC_INSTALL + 'echo keep-this\n',
            'attic login ci https://attic.example "$TOKEN"',
        ):
            with self.subTest(command=command):
                document = {'jobs': {'custom': {'if': PUBLISH_GUARD, 'steps': [{'run': command}]}}}
                with self.assertRaises(migration.MigrationError):
                    migration.migrate_document(document)

    def test_rejects_unknown_credential_steps_and_inherited_environments(self):
        for document in (
            {'jobs': {'custom': {'steps': [{'env': {'ATTIC_TOKEN': '${{ secrets.ATTIC_TOKEN }}'}, 'run': './custom-publisher'}]}}},
            {'env': {'ATTIC_TOKEN': 'synthetic'}, 'jobs': {'custom': {'steps': []}}},
            {'jobs': {'custom': {'uses': migration.CALLER + '1' * 40, 'secrets': 'inherit'}}},
        ):
            with self.subTest(document=document), self.assertRaises(migration.MigrationError):
                migration.migrate_document(document)

    def test_rejects_unguarded_publication_and_mismatched_dual_guards(self):
        document = native_workflow()
        document['jobs']['publish'].pop('if')
        with self.assertRaises(migration.MigrationError):
            migration.migrate_document(document)
        document = native_workflow()
        document['jobs']['publish']['steps'][-1]['if'] = '${{ always() }}'
        with self.assertRaisesRegex(migration.MigrationError, 'guards do not match'):
            migration.migrate_document(document)

    def test_rejects_references_to_removed_client_step_outputs(self):
        document = native_workflow()
        document['jobs']['publish']['steps'].append({'run': 'echo "${{ steps.attic.outputs.binary }}"'})
        with self.assertRaisesRegex(migration.MigrationError, 'referenced outputs'):
            migration.migrate_document(document)

    def test_converts_attic_public_nix_configuration_without_touching_other_lines(self):
        document = native_workflow()
        settings = document['jobs']['build']['steps'][0]['with']
        settings['extra-conf'] = 'max-jobs = 1\nextra-substituters = ${{ vars.ATTIC_SUBSTITUTER }}\nextra-trusted-public-keys = ${{ vars.ATTIC_PUBLIC_KEY }}\n'
        after, _ = migration.migrate_document(document)
        config = after['jobs']['build']['steps'][0]['with']['extra-conf']
        self.assertEqual(config, 'max-jobs = 1\nextra-substituters = \nextra-trusted-public-keys = ' + migration.KEY + '\n')
        settings['extra-conf'] = 'extra-substituters = https://other.example ${{ vars.ATTIC_SUBSTITUTER }}\n'
        with self.assertRaisesRegex(migration.MigrationError, 'Mixed Attic Nix configuration'):
            migration.migrate_document(document)


@unittest.skipUnless(HAS_YAML, 'PyYAML is required for workflow serialization tests')
class WorkflowFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / '.github/workflows'
        self.folder.mkdir(parents=True)

    def write(self, name, document):
        path = self.folder / name
        path.write_text(migration.dumps(document))
        return path

    def test_yaml_12_on_booleans_multiline_runs_and_numbers_roundtrip(self):
        text = '''name: Example
on:
  push:
env:
  TEXT_ON: on
  TEXT_OFF: off
  TEXT_YES: yes
  TEXT_NO: no
  DECIMAL: 0123
  OCTAL: 0o17
  SCIENTIFIC: 1e3
  DATE: 2026-11-07
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: |
          echo first
          echo second
'''
        loaded = migration.loads(text)
        self.assertIn('on', loaded)
        self.assertNotIn(True, loaded)
        self.assertEqual(loaded['env'], {'TEXT_ON': 'on', 'TEXT_OFF': 'off', 'TEXT_YES': 'yes', 'TEXT_NO': 'no', 'DECIMAL': 123, 'OCTAL': 15, 'SCIENTIFIC': 1000.0, 'DATE': '2026-11-07'})
        dumped = migration.dumps(loaded)
        self.assertIn('run: |', dumped)
        self.assertEqual(migration.loads(dumped), loaded)

    def test_check_never_writes_and_apply_is_byte_idempotent(self):
        path = self.write('native.yml', native_workflow())
        generated = self.folder / 'release-channels.yml'
        generated.write_text('# installer-owned; deliberately not parsed\non: [unclosed')
        before = path.read_bytes()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(migration.main(['--directory', str(self.root), '--check']), 1)
        self.assertEqual(path.read_bytes(), before)
        report = migration.migrate_directory(self.root)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['changed'], ['.github/workflows/native.yml'])
        self.assertEqual(report['skipped'], ['.github/workflows/release-channels.yml'])
        once = path.read_bytes()
        report = migration.migrate_directory(self.root)
        self.assertEqual(report['changed'], [])
        self.assertEqual(path.read_bytes(), once)
        self.assertEqual(generated.read_text(), '# installer-owned; deliberately not parsed\non: [unclosed')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(migration.main(['--directory', str(self.root), '--check']), 0)

    def test_one_unknown_workflow_prevents_all_directory_writes(self):
        good = self.write('a-native.yml', native_workflow())
        bad = self.write('z-unknown.yml', {'jobs': {'custom': {'steps': [{'env': {'ATTIC_TOKEN': 'synthetic'}, 'run': './custom-publisher'}]}}})
        snapshot = {path: path.read_bytes() for path in (good, bad)}
        report = migration.migrate_directory(self.root)
        self.assertTrue(report['errors'])
        self.assertEqual({path: path.read_bytes() for path in (good, bad)}, snapshot)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(migration.main(['--directory', str(self.root)]), 2)

    def test_duplicate_keys_invalid_yaml_and_nonexact_controller_pins_fail_closed(self):
        path = self.folder / 'native.yml'
        for text in ('on: {push: null}\non: {pull_request: null}\njobs: {}\n', 'on: [unclosed\n'):
            path.write_text(text)
            report = migration.migrate_directory(self.root)
            self.assertTrue(report['errors'])
            self.assertEqual(path.read_text(), text)
        for revision in ('main', 'a' * 39, 'A' * 40):
            with self.subTest(revision=revision), self.assertRaises(migration.MigrationError):
                migration.migrate_directory(self.root, revision)

    def test_current_audited_native_shapes_migrate_or_identify_manual_cache_only(self):
        snapshots = list(Path('/tmp').glob('niks3-audit-github-*.json'))
        if not snapshots:
            self.skipTest('Optional current-audit snapshots are unavailable; inline shapes remain covered')
        checked = 0
        for snapshot in snapshots:
            data = json.loads(snapshot.read_text())
            if data['repo'] == 'dave-fork-ci':
                continue  # Controller definition is maintained directly, not an owning caller.
            for filename, text in data.get('files', {}).items():
                if Path(filename).name == 'release-channels.yml' or not any(needle in text for needle in ('ci/configure-attic-read.py', 'ci/push-attic.py', migration.CALLER)):
                    continue
                with self.subTest(repository=data['repo'], workflow=filename):
                    if filename.endswith('/cache-only.yml'):
                        with self.assertRaisesRegex(migration.MigrationError, 'build-cache-only.py requires manual'):
                            migration.migrate_document(migration.loads(text), '2' * 40)
                    else:
                        after, _ = migration.migrate_document(migration.loads(text), '2' * 40)
                        self.assertEqual(migration.loads(migration.dumps(after)), after)
                        twice, _ = migration.migrate_document(after, '2' * 40)
                        self.assertEqual(after, twice)
                    checked += 1
        self.assertGreater(checked, 10)


if __name__ == '__main__':
    unittest.main()
