#!/usr/bin/env python3
"""Prepare and promote immutable upstream merge candidates without executing them."""
import argparse
import base64
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
from history_filter import filter_history

SHA = re.compile(r"[a-f0-9]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ATTR = re.compile(r"[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*")
RUNNERS = {'aarch64-darwin': 'macos-15', 'x86_64-linux': 'ubuntu-24.04',
           'aarch64-linux': 'ubuntu-24.04-arm'}
PROTECTED = ['.github', '.github/**', '.forgejo', '.forgejo/**', '.gitmodules',
             '.fork-ci.json', '.fork-ci', '.fork-ci/**']
STATE = '.fork-ci/upstream-state.json'


class Blocked(RuntimeError):
    pass


def git(repo, *args, check=True, extra_env=None, input_data=None):
    # No inherited global helpers, filters, tracing, hooks or file transports.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('GIT_', 'GH_', 'GITHUB_TOKEN', 'ATTIC_TOKEN'))}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0')
    env.update(extra_env or {})
    result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
                             '-c', 'core.fsmonitor=false', '-c', 'protocol.file.allow=never',
                             *args], cwd=repo, env=env, capture_output=True, input=input_data)
    if check and result.returncode:
        raise Blocked('Git operation failed; resolve conflicts or repository policy locally')
    return result


def text(repo, *args):
    return git(repo, *args).stdout.decode().strip()


def ref(value):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise Blocked('Invalid branch or tag')
    if value.startswith(('-', '/')) or any(c.isspace() or ord(c) < 32 for c in value):
        raise Blocked('Invalid branch or tag')
    result = subprocess.run(['git', 'check-ref-format', 'refs/heads/' + value], capture_output=True)
    if result.returncode:
        raise Blocked('Invalid branch or tag')
    return value


def validate_policy(policy):
    if policy.get('version') != 1 or not REPO.fullmatch(policy.get('upstream', '')):
        raise Blocked('Policy version/upstream must identify a public GitHub repository')
    ref(policy.get('branch', 'main'))
    if policy.get('track') not in ('branch', 'release', 'both'):
        raise Blocked('track must be branch, release or both')
    author = policy.get('author', {})
    if any(not isinstance(author.get(k), str) or not author[k].strip()
           or any(c in author[k] for c in '\r\n<>') for k in ('name', 'email')):
        raise Blocked('Configure an explicit Git author')
    matrix = policy.get('matrix', [])
    if not matrix or len(matrix) > 3 or len({r['system'] for r in matrix}) != len(matrix):
        raise Blocked('Configure one unique row per supported platform')
    for row in matrix:
        if row.get('system') not in RUNNERS or not row.get('packages'):
            raise Blocked('Unsupported platform or empty package list')
        if not all(isinstance(a, str) and ATTR.fullmatch(a)
                   for a in row['packages'] + row.get('checks', [])):
            raise Blocked('Package/check names must be plain flake attribute paths')
        if not isinstance(row.get('tests', []), list):
            raise Blocked('Tests must be argument vectors')
        for command in row.get('tests', []):
            if not isinstance(command, list) or not command or not all(
                    isinstance(a, str) and a and '\0' not in a for a in command):
                raise Blocked('Tests must be nonempty argument vectors')
    for path in policy.get('required_paths', []):
        if not isinstance(path, str) or path.startswith('/') or '..' in path.split('/'):
            raise Blocked('Required paths must stay within the fork')
    for pattern in policy.get('protected_paths', []):
        if not isinstance(pattern, str) or not pattern:
            raise Blocked('Invalid protected path pattern')
    excluded = policy.get('history_exclude', [])
    if not isinstance(excluded, list) or len(excluded) > 100:
        raise Blocked('History exclusions must be a bounded list of literal paths')
    for path in excluded:
        if (not isinstance(path, str) or not path or path.startswith(('/', '-'))
                or any(p in ('', '.', '..', '.git') for p in path.split('/'))
                or any(c in path for c in '*?[]\\\r\n\0')
                or any(fnmatch.fnmatchcase(path, p) or p.startswith(path + '/')
                       for p in PROTECTED + ['flake.nix'] + policy.get('required_paths', [])
                       + policy.get('protected_paths', []))):
            raise Blocked('History exclusions cannot hide control/protected files')
    return policy


class NoMetadataRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Authorization belongs only to the API origin, including on redirects.
        raise Blocked('GitHub metadata redirect requires review')


def api(endpoint):
    if not re.fullmatch(r'repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/latest', endpoint):
        raise Blocked('Invalid public release metadata endpoint')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'dave-fork-ci'}
    token = os.environ.get('GH_TOKEN')
    if token:
        if any(c.isspace() or ord(c) < 32 for c in token):
            raise Blocked('Invalid GitHub job token')
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request('https://api.github.com/' + endpoint, headers=headers)
    try:
        with urllib.request.build_opener(NoMetadataRedirect()).open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return None
        raise Blocked('Public GitHub metadata unavailable or rate limited') from None
    except urllib.error.URLError:
        raise Blocked('Public GitHub metadata unavailable') from None


def latest_release(repository):
    # /latest follows GitHub's latest stable release designation; prereleases
    # and drafts are deliberately excluded, not lexically sorted as versions.
    release = api('repos/' + repository + '/releases/latest')
    if release is None:
        return None
    if release.get('draft') or release.get('prerelease'):
        raise Blocked('Unexpected nonstable latest release')
    return {'id': release['id'], 'tag': ref(release['tag_name'])}


def tree_bytes(repo, revision, path, optional=False):
    result = git(repo, 'show', revision + ':' + path, check=False)
    if result.returncode:
        if optional:
            return None
        raise Blocked('Required file is missing')
    return result.stdout


def changes(repo, base, head):
    return [p.decode() for p in git(repo, 'diff', '--name-only', '-z', base, head).stdout.split(b'\0') if p]


def guard(repo, base, head, policy, allow_state=False):
    patterns = PROTECTED + policy.get('protected_paths', [])
    blocked = [p for p in changes(repo, base, head)
               if not (allow_state and p == STATE)
               and any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)]
    if blocked:
        raise Blocked('Upstream changes control/protected files; local review is required')
    for path in ['flake.nix', '.fork-ci.json', *policy.get('required_paths', [])]:
        mode = text(repo, 'ls-tree', head, '--', path).split(' ', 1)[0]
        if mode not in ('100644', '100755', '040000'):
            raise Blocked('Required fork path was removed or replaced by a symlink/submodule')


def merge_candidate(repo, base, targets, policy, state):
    """Only Git's three-way merge; preserve every fork commit and stop on conflict."""
    git(repo, 'checkout', '--detach', base)
    timestamp = max(int(text(repo, 'show', '-s', '--format=%ct', rev))
                    for rev in [base, *targets]) + 1
    env = {'GIT_AUTHOR_NAME': policy['author']['name'],
           'GIT_AUTHOR_EMAIL': policy['author']['email'],
           'GIT_COMMITTER_NAME': policy['author']['name'],
           'GIT_COMMITTER_EMAIL': policy['author']['email'],
           'GIT_AUTHOR_DATE': '@' + str(timestamp) + ' +0000',
           'GIT_COMMITTER_DATE': '@' + str(timestamp) + ' +0000'}
    for target in dict.fromkeys(targets):
        if git(repo, 'merge-base', '--is-ancestor', target, 'HEAD', check=False).returncode == 0:
            continue
        git(repo, 'merge', '--no-ff', '--no-commit', target, extra_env=env)
        guard(repo, base, 'HEAD', policy)
        # A pending merge is in the index; HEAD still points at its first parent.
        tree = text(repo, 'write-tree')
        guard(repo, base, tree, policy)
        git(repo, 'commit', '--no-gpg-sign', '-m', 'Merge upstream ' + target, extra_env=env)
    guard(repo, base, 'HEAD', policy)
    file = repo / STATE
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(state, sort_keys=True, indent=2) + '\n')
    git(repo, 'add', '--', STATE)
    git(repo, 'commit', '--no-gpg-sign', '-m', 'ci: record reviewed upstream candidate', extra_env=env)
    return text(repo, 'rev-parse', 'HEAD')


def push(repo, url, source, destination, expected=None):
    token = os.environ.get('GH_TOKEN', '')
    if not token:
        raise Blocked('Repository-scoped write token is missing')
    header = base64.b64encode(('x-access-token:' + token).encode()).decode()
    auth = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
            'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + header}
    if expected is not None:
        advertised = git(repo, 'ls-remote', url, destination, extra_env=auth).stdout.split()
        current = advertised[0].decode() if advertised else ''
        if current != expected and current != source:
            raise Blocked('Destination moved; refusing to overwrite concurrent work')
    # The server also enforces a normal fast-forward push. No force option or
    # branch-protection bypass is requested, even after checking the expected ref.
    git(repo, 'push', '--porcelain', url, source + ':' + destination, extra_env=auth)


def emit(values):
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as stream:
            for key, value in values.items():
                stream.write(key + '=' + (value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))) + '\n')


def prepare(args):
    if not REPO.fullmatch(args.repository):
        raise Blocked('Invalid target GitHub repository')
    target = ref(args.target)
    url = 'https://github.com/' + args.repository + '.git'
    repo = args.work
    repo.mkdir(parents=True, exist_ok=False)
    git(repo, 'init', '--quiet')
    git(repo, 'fetch', '--no-tags', url, 'refs/heads/' + target)
    base = text(repo, 'rev-parse', 'FETCH_HEAD')
    raw = tree_bytes(repo, base, '.fork-ci.json')
    policy = validate_policy(json.loads(raw))
    upstream = 'https://github.com/' + policy['upstream'] + '.git'
    state = {'version': 1, 'upstream': policy['upstream'], 'track': policy['track']}
    targets = []
    if policy['track'] in ('branch', 'both'):
        git(repo, 'fetch', '--no-tags', upstream, 'refs/heads/' + policy.get('branch', 'main'))
        state['branch'] = text(repo, 'rev-parse', 'FETCH_HEAD^{commit}')
        targets.append(state['branch'])
    if policy['track'] in ('release', 'both'):
        release = latest_release(policy['upstream'])
        if release:
            git(repo, 'fetch', '--no-tags', upstream, 'refs/tags/' + release['tag'])
            release['sha'] = text(repo, 'rev-parse', 'FETCH_HEAD^{commit}')
            targets.append(release['sha'])
        state['release'] = release
    previous = tree_bytes(repo, base, STATE, optional=True)
    excluded = policy.get('history_exclude', [])
    if excluded:
        state['history_exclude'] = sorted(set(excluded))
        filtered = {revision: filter_history(repo, revision, excluded, git)
                    for revision in dict.fromkeys(targets)}
        targets = [filtered[revision] for revision in targets]
        if state.get('branch'):
            state['filtered_branch'] = filtered[state['branch']]
        if state.get('release'):
            state['release']['filtered_sha'] = filtered[state['release']['sha']]
    if (previous is not None and json.loads(previous) == state) or not targets:
        emit({'changed': 'false'})
        return
    # Refuse rewound upstream branches or retagged releases once recorded.
    if previous:
        old = json.loads(previous)
        if old.get('upstream') != policy['upstream'] or old.get('track') != policy['track']:
            raise Blocked('Upstream policy changed; review/reset its recorded state locally')
        if old.get('history_exclude', []) != state.get('history_exclude', []):
            raise Blocked('History exclusion policy changed; local review required')
        if old.get('branch') and state.get('branch') and git(repo, 'merge-base', '--is-ancestor', old['branch'], state['branch'], check=False).returncode:
            raise Blocked('Upstream branch was rewritten; local review required')
        if old.get('release') and state.get('release') and old['release']['tag'] == state['release']['tag'] and old['release']['sha'] != state['release']['sha']:
            raise Blocked('Upstream release tag moved; local review required')
    candidate = merge_candidate(repo, base, targets, policy, state)
    branch = 'sync/upstream/' + candidate
    push(repo, url, candidate, 'refs/heads/' + branch, expected='')
    emit({'changed': 'true', 'base': base, 'candidate': candidate, 'candidate_branch': branch,
          'matrix': {'include': [{'system': r['system'], 'runner': RUNNERS[r['system']]}
                                 for r in policy['matrix']]}})


def verify(repo, base, candidate, target, policy):
    if not SHA.fullmatch(base) or not SHA.fullmatch(candidate):
        raise Blocked('Expected immutable Git revisions')
    if target != base or git(repo, 'merge-base', '--is-ancestor', base, candidate, check=False).returncode:
        raise Blocked('Main changed or candidate is not a fast-forward descendant')
    guard(repo, base, candidate, policy, allow_state=True)
    state = json.loads(tree_bytes(repo, candidate, STATE))
    if state['upstream'] != policy['upstream'] or state['track'] != policy['track']:
        raise Blocked('Candidate does not match the configured upstream')
    excluded = sorted(set(policy.get('history_exclude', [])))
    if state.get('history_exclude', []) != excluded:
        raise Blocked('Candidate history filter does not match policy')
    revisions = [state.get('branch'), (state.get('release') or {}).get('sha')]
    if excluded:
        filtered = [state.get('filtered_branch'), (state.get('release') or {}).get('filtered_sha')]
        for original, expected in zip(revisions, filtered):
            if original:
                if not SHA.fullmatch(original) or not isinstance(expected, str) or not SHA.fullmatch(expected):
                    raise Blocked('Invalid filtered upstream revision')
                git(repo, 'fetch', '--no-tags', 'https://github.com/' + policy['upstream'] + '.git', original)
                if filter_history(repo, original, excluded, git) != expected:
                    raise Blocked('Candidate filtered history does not match upstream')
        revisions = filtered
    for sha in revisions:
        if sha and (not SHA.fullmatch(sha) or git(repo, 'merge-base', '--is-ancestor', sha, candidate, check=False).returncode):
            raise Blocked('Candidate does not contain the recorded upstream commit')


def promote(args):
    if not REPO.fullmatch(args.repository):
        raise Blocked('Invalid GitHub repository')
    url = 'https://github.com/' + args.repository + '.git'
    repo = args.work
    repo.mkdir(parents=True, exist_ok=False)
    git(repo, 'init', '--quiet')
    git(repo, 'fetch', '--no-tags', url, 'refs/heads/' + ref(args.target))
    target = text(repo, 'rev-parse', 'FETCH_HEAD')
    git(repo, 'fetch', '--no-tags', url, 'refs/heads/sync/upstream/' + args.candidate)
    if text(repo, 'rev-parse', 'FETCH_HEAD') != args.candidate:
        raise Blocked('Candidate branch changed after its build')
    policy = validate_policy(json.loads(tree_bytes(repo, args.base, '.fork-ci.json')))
    verify(repo, args.base, args.candidate, target, policy)
    push(repo, url, args.candidate, 'refs/heads/' + args.target, expected=args.base)
    emit({'promoted': args.candidate})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'promote'])
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--target', default='main')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--base', default='')
    parser.add_argument('--candidate', default='')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.phase == 'promote' and (not SHA.fullmatch(args.base) or not SHA.fullmatch(args.candidate)):
            raise Blocked('Promotion requires exact revisions')
        (prepare if args.phase == 'prepare' else promote)(args)
    except (Blocked, OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        # Do not emit subprocess output, HTTP bodies or token-bearing values.
        print('Fork maintenance stopped: ' + (str(error) if isinstance(error, Blocked) else 'invalid input or unavailable dependency'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
