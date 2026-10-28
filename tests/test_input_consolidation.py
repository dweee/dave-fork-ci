import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('automation', Path(__file__).resolve().parents[1] / 'ci/automation-forge.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class Consolidation(unittest.TestCase):
    def setUp(self):
        self.rules = {'forge': 'github', 'repository': 'owner/tool', 'branch': 'main', 'inputCheckWorkflows': ['native.yml']}

    def pr(self, number, **changes):
        value = {'number': number, 'body': a.INPUT_MARKER, 'base': {'ref': 'main'},
                 'head': {'ref': 'automation/input-updates-' + str(number), 'sha': 'a' * 40,
                          'repo': {'full_name': 'owner/tool'}}}
        value.update(changes)
        return value

    def test_only_same_repo_marked_target_candidates_are_closed(self):
        current = self.pr(3)
        others = [self.pr(1), self.pr(2, body='manual review'), self.pr(4, base={'ref': 'other'}),
                  self.pr(5, head={'ref': 'automation/input-updates-5', 'repo': {'full_name': 'fork/tool'}})]
        with patch.object(a, 'api', return_value=[{'filename': 'flake.lock'}]) as api:
            a.consolidate_input_prs(self.rules, '/repos/owner/tool', others + [current], current)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args.args, (self.rules, 'PATCH', '/repos/owner/tool/pulls/1', {'state': 'closed'}))

    def test_manual_source_fixes_and_truncated_files_are_preserved(self):
        for files in [[{'filename': 'src/lib.rs'}], [{'filename': 'flake.lock'}] * 100, []]:
            with self.subTest(files=len(files)), patch.object(a, 'api', return_value=files) as api:
                a.consolidate_input_prs(self.rules, '/repos/owner/tool', [self.pr(1)], self.pr(2))
                self.assertEqual(api.call_count, 1)

    def test_missing_checks_are_dispatched_once_on_candidate_branch(self):
        with patch.object(a, 'api', return_value={'workflow_runs': []}) as api:
            a.dispatch_input_checks(self.rules, '/repos/owner/tool', self.pr(1))
        self.assertEqual(api.call_args.args, (self.rules, 'POST', '/repos/owner/tool/actions/workflows/native.yml/dispatches', {'ref': 'automation/input-updates-1'}))
        with patch.object(a, 'api', return_value={'workflow_runs': [{'id': 1, 'conclusion': 'failure'}]}) as api:
            a.dispatch_input_checks(self.rules, '/repos/owner/tool', self.pr(1))
        self.assertEqual(api.call_count, 1)

    def test_dispatch_failure_keeps_old_candidates_open(self):
        with patch.object(a, 'api', side_effect=RuntimeError('dispatch refused')) as api:
            with self.assertRaises(RuntimeError):
                a.dispatch_input_checks(self.rules, '/repos/owner/tool', self.pr(1))
            self.assertFalse(any(c.args[1] == 'PATCH' for c in api.call_args_list))

    def test_workflow_path_cannot_escape_reviewed_directory(self):
        rules = dict(self.rules, inputCheckWorkflows=['../unsafe.yml'])
        with patch.object(a, 'api') as api, self.assertRaises(ValueError):
            a.dispatch_input_checks(rules, '/repos/owner/tool', self.pr(1))
        api.assert_not_called()
