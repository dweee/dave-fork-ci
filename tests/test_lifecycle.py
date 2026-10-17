"""Exercise prepare/promote over local HTTP Git repositories; no external service."""
from functools import partial
import http.server
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_fork_ci as fixtures
import fork_ci as ci


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class LifecycleTests(fixtures.MergeTests):
    # Only lifecycle cases are repeated here, not the parent merge unit cases.
    def setUp(self):
        super().setUp()
        self.fork = Path(self.tmp.name) / 'fork.git'
        self.up = Path(self.tmp.name) / 'upstream.git'
        self.target = self.upstream()
        for bare, revision in [(self.fork, self.base), (self.up, self.target)]:
            subprocess.run(['git', 'clone', '--bare', '--quiet', str(self.repo), str(bare)], check=True)
            subprocess.run(['git', '--git-dir', str(bare), 'update-ref', 'refs/heads/main', revision], check=True)
            subprocess.run(['git', '--git-dir', str(bare), 'symbolic-ref', 'HEAD', 'refs/heads/main'], check=True)
            subprocess.run(['git', '--git-dir', str(bare), 'update-server-info'], check=True)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                partial(QuietHandler, directory=self.tmp.name))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        original = ci.git

        def local_git(repo, *args, **kwargs):
            mapping = {'https://github.com/example/fork.git': 'fork.git',
                       'https://github.com/example/project.git': 'upstream.git'}
            args = [f'http://127.0.0.1:{self.server.server_port}/' + mapping[a] if a in mapping else a for a in args]
            return original(repo, *args, **kwargs)

        def local_push(repo, url, source, destination, expected=None):
            subprocess.run(['git', 'push', '--quiet', str(self.fork), source + ':' + destination], cwd=repo, check=True)
            subprocess.run(['git', '--git-dir', str(self.fork), 'update-server-info'], check=True)

        for mock in [patch.object(ci, 'git', side_effect=local_git), patch.object(ci, 'push', side_effect=local_push),
                     patch.object(ci, 'latest_release', return_value=None)]:
            mock.start()
            self.addCleanup(mock.stop)

    def prepare(self, suffix):
        outputs = {}
        with patch.object(ci, 'emit', side_effect=outputs.update):
            ci.prepare(SimpleNamespace(repository='example/fork', target='main',
                       work=Path(self.tmp.name) / suffix))
        return outputs

    def test_lifecycle_success_then_noop(self):
        outputs = self.prepare('first')
        self.assertEqual(outputs['changed'], 'true')
        ci.promote(SimpleNamespace(repository='example/fork', target='main',
                   work=Path(self.tmp.name) / 'promote', base=outputs['base'], candidate=outputs['candidate']))
        self.assertEqual(self.prepare('second'), {'changed': 'false'})

    def test_filtered_history_promotes_without_reintroducing_upstream_artifacts(self):
        ci.git(self.repo, 'checkout', '--detach', self.base)
        policy = json.loads((self.repo / '.fork-ci.json').read_text())
        policy['history_exclude'] = ['.claude']
        (self.repo / '.fork-ci.json').write_text(json.dumps(policy))
        base = self.commit('configure artifact-free upstream history')
        ci.git(self.repo, 'checkout', '--detach', self.target)
        (self.repo / '.claude').mkdir()
        (self.repo / '.claude/chat.jsonl').write_text('synthetic conversation fixture\n')
        raw_upstream = self.commit('upstream introduces private artifact')
        for bare, revision in [(self.fork, base), (self.up, raw_upstream)]:
            subprocess.run(['git', 'push', '--quiet', '--force', str(bare), revision + ':refs/heads/main'], cwd=self.repo, check=True)
            subprocess.run(['git', '--git-dir', str(bare), 'update-server-info'], check=True)
        outputs = self.prepare('filtered-first')
        candidate = outputs['candidate']
        work = Path(self.tmp.name) / 'filtered-first'
        self.assertNotIn('.claude', ci.text(work, 'rev-list', '--objects', candidate))
        self.assertNotIn(raw_upstream, ci.text(work, 'rev-list', candidate))
        ci.promote(SimpleNamespace(repository='example/fork', target='main',
                   work=Path(self.tmp.name) / 'filtered-promote', base=base, candidate=candidate))
        self.assertEqual(self.prepare('filtered-noop'), {'changed': 'false'})

    def test_release_publication_rebuilds_already_merged_source(self):
        first = self.prepare('first')
        ci.promote(SimpleNamespace(repository='example/fork', target='main',
                   work=Path(self.tmp.name) / 'promote', base=first['base'], candidate=first['candidate']))
        subprocess.run(['git', '--git-dir', str(self.up), 'update-ref', 'refs/tags/v1.0', self.target], check=True)
        subprocess.run(['git', '--git-dir', str(self.up), 'update-server-info'], check=True)
        with patch.object(ci, 'latest_release', return_value={'id': 1, 'tag': 'v1.0'}):
            second = self.prepare('release')
        self.assertEqual(second['changed'], 'true')
        self.assertNotEqual(first['candidate'], second['candidate'])

    def test_rewritten_upstream_requires_review(self):
        first = self.prepare('first')
        ci.promote(SimpleNamespace(repository='example/fork', target='main',
                   work=Path(self.tmp.name) / 'promote', base=first['base'], candidate=first['candidate']))
        subprocess.run(['git', '--git-dir', str(self.up), 'update-ref', 'refs/heads/main', self.upstream_base], check=True)
        subprocess.run(['git', '--git-dir', str(self.up), 'update-server-info'], check=True)
        with self.assertRaises(ci.Blocked):
            self.prepare('rewound')

    def test_moved_release_tag_requires_review(self):
        subprocess.run(['git', '--git-dir', str(self.up), 'update-ref', 'refs/tags/v1.0', self.target], check=True)
        subprocess.run(['git', '--git-dir', str(self.up), 'update-server-info'], check=True)
        with patch.object(ci, 'latest_release', return_value={'id': 1, 'tag': 'v1.0'}):
            first = self.prepare('release-first')
            ci.promote(SimpleNamespace(repository='example/fork', target='main',
                       work=Path(self.tmp.name) / 'promote', base=first['base'], candidate=first['candidate']))
            subprocess.run(['git', '--git-dir', str(self.up), 'update-ref', 'refs/tags/v1.0', self.upstream_base], check=True)
            subprocess.run(['git', '--git-dir', str(self.up), 'update-server-info'], check=True)
            with self.assertRaises(ci.Blocked):
                self.prepare('retagged')


# The base class supplies fixture helpers; its unit cases run in their own suite.
for name in list(vars(fixtures.MergeTests)):
    if name.startswith('test_'):
        setattr(LifecycleTests, name, None)


if __name__ == '__main__':
    unittest.main()
