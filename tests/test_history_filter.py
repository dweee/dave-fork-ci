import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_fork_ci as fixtures
import fork_ci as ci
from history_filter import filter_history


class HistoryFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        ci.git(self.repo, 'init', '-q')
        ci.git(self.repo, 'config', 'user.name', 'Original upstream author')
        ci.git(self.repo, 'config', 'user.email', 'upstream@example.org')

    def commit(self, message):
        ci.git(self.repo, 'add', '--all')
        ci.git(self.repo, 'commit', '--no-gpg-sign', '-qm', message)
        return ci.text(self.repo, 'rev-parse', 'HEAD')

    def test_deleted_historical_artifact_is_absent_and_authorship_preserved(self):
        (self.repo / 'source.txt').write_text('public source\n')
        (self.repo / '.claude').mkdir()
        (self.repo / '.claude/chat.jsonl').write_text('synthetic conversation fixture\n')
        raw_base = self.commit('upstream source and local artifact')
        (self.repo / '.claude/chat.jsonl').unlink()
        raw_head = self.commit('upstream removes artifact')
        filtered = filter_history(self.repo, raw_head, ['.claude'], ci.git)
        objects = ci.text(self.repo, 'rev-list', '--objects', filtered)
        self.assertNotIn('.claude', objects)
        self.assertNotEqual(filtered, raw_head)
        self.assertNotIn(raw_base, ci.text(self.repo, 'rev-list', filtered))
        self.assertEqual(ci.text(self.repo, 'show', '-s', '--format=%an <%ae>%n%at%n%ct%n%B', filtered),
                         ci.text(self.repo, 'show', '-s', '--format=%an <%ae>%n%at%n%ct%n%B', raw_head))
        self.assertEqual(filtered, filter_history(self.repo, raw_head, ['.claude'], ci.git))

    def test_later_filtered_updates_share_filtered_ancestry_and_keep_source(self):
        (self.repo / 'source.txt').write_text('base\n')
        base = self.commit('clean upstream base')
        self.assertEqual(filter_history(self.repo, base, ['.claude'], ci.git), base)
        (self.repo / '.claude').mkdir()
        (self.repo / '.claude/state.json').write_text('{}\n')
        first = filter_history(self.repo, self.commit('add artifact'), ['.claude'], ci.git)
        (self.repo / 'source.txt').write_text('new public behavior\n')
        second = filter_history(self.repo, self.commit('new behavior'), ['.claude'], ci.git)
        ci.git(self.repo, 'merge-base', '--is-ancestor', first, second)
        self.assertEqual(ci.tree_bytes(self.repo, second, 'source.txt'), b'new public behavior\n')
        self.assertNotIn('.claude', ci.text(self.repo, 'rev-list', '--objects', second))

    def test_exclusion_policy_cannot_hide_controls_or_required_source(self):
        for path in ['.github', '.github/workflows', '.fork-ci.json', '.gitmodules',
                     'flake.nix', '../source', '.git', 'nix', '*', '.claude/']:
            policy = copy.deepcopy(fixtures.POLICY)
            policy['history_exclude'] = [path]
            with self.subTest(path=path), self.assertRaises(ci.Blocked):
                ci.validate_policy(policy)
        policy = copy.deepcopy(fixtures.POLICY)
        policy['history_exclude'] = ['.claude', 'docs/internal-report.md']
        self.assertEqual(ci.validate_policy(policy), policy)
