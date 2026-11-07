#!/usr/bin/env python3
"""Install reviewed CI helpers into an owning repository (requires PyYAML).

Run locally with --directory, --forge, --repository, --branch and --policy.
The input policy is explicit; this tool never guesses publication ownership.
"""
import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
import shutil

def multiline(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)

HERE = Path(__file__).resolve().parent
CHECKOUT = 'actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803'
INSTALL = 'DeterminateSystems/determinate-nix-action@92957199488000b908fdad19f63711000965d8f3'
INTEL_DARWIN_INSTALL = 'nixbuild/nix-quick-install-action@9f63be77f412a248c9d9a65a4c82cf066cdf8f0c'
# Paths that cannot change a built package. A repository that sets
# githubCounterpartCi must still confirm none of these feed its build.
COUNTERPART_PATHS_IGNORE = ['.forgejo/**', '.github/**', 'docs/**', '**/*.md', 'LICENSE*', 'COPYING*', 'DCO']
COUNTERPART_NOTE = '''
This repository sets `githubCounterpartCi`: a GitHub Actions counterpart already
builds and checks it, so no Forgejo `nix.yml` is generated (an existing copy is
removed) and the release workflow does not run for pull requests. Pushes to the
source branch that only touch the paths listed under `paths-ignore` in
`release-channels.yml` skip the workflow; `tested` stays at its last verified
commit until the next qualifying push, the nightly run or an owner dispatch
publishes the current head. Confirm that no ignored path feeds a package.
'''
# The Attic host (icicle) is reachable at the runner-network address only from
# jobs on the icicle-hosted `nix` runner. Every other runner keeps the public URL.
INTERNAL_ATTIC = 'http://172.29.0.1:8080'
# braco can reach the same listener over the tailnet, but its runner unit denies
# the tailnet range. Once that allows icicle, adding 'braco': TAILNET_ATTIC below
# is the whole installer change; the helpers already accept this origin.
TAILNET_ATTIC = 'http://icicle.eel-ling.ts.net:8080'
CACHE_SERVERS = {'nix': INTERNAL_ATTIC}
HELPERS = ['release-channel.py', 'test-release-channel.py', 'update-inputs.py',
           'update-monitor.py', 'test-update-automation.py', 'automation-forge.py', 'configure-attic-read.py',
           'configure-niks3.py', 'push-niks3.py', 'test-push-niks3.py', 'niks3-oidc-token.py', 'install-niks3.sh']


# Bounded Nix downloads: fail fast on a dead connection and retry transient DNS,
# cache and CDN errors instead of stalling a job for hours.
NIX_TRANSPORT_CONF = 'connect-timeout = 20\nstalled-download-timeout = 120\ndownload-attempts = 5\n'
NIX_CONF_FORGEJO = ('experimental-features = nix-command flakes\naccept-flake-config = false\n'
                    'sandbox = false\nfilter-syscalls = false\n' + NIX_TRANSPORT_CONF)


def expr(value):
    return '${{ ' + value + ' }}'


def trusted_branch_ref(forge, branch):
    full = "github.ref == 'refs/heads/" + branch + "'"
    if forge == 'github':
        return full
    # Forgejo's scheduler stores the default branch as a bare ref. Push and
    # dispatch runs still use refs/heads/. Accept the bare form only on cron;
    # PR refs, tags and other branches must not enter credentialed jobs.
    return "(" + full + " || (github.event_name == 'schedule' && github.ref == '" + branch + "'))"


def schedule_slots(repository):
    """Stable per-repository schedule offsets (never Python's randomised hash()).

    Every repository keeps the same cadence, but its slot is derived from a
    SHA-256 of `owner/name`, so runs do not all queue in the same minute.
    Returns (release hour 0-23 UTC, release minute, updates hour offset 0-5, updates minute).
    The heavy release run is spread over the whole day, not just the night.
    """
    digest = hashlib.sha256(repository.encode()).digest()
    minute_of_day = int.from_bytes(digest[0:2], 'big') % 1440
    return minute_of_day // 60, minute_of_day % 60, digest[2] % 6, digest[3] % 60


def release_cron(repository):
    hour, minute, _, _ = schedule_slots(repository)
    return f'{minute} {hour} * * *'


def updates_cron(repository):
    _, _, offset, minute = schedule_slots(repository)
    return f'{minute} ' + ','.join(str(offset + 6 * slot) for slot in range(4)) + ' * * *'


UNACQUIRED_RETRY = '''set -euo pipefail
jobs=$(gh api --paginate "repos/$REPOSITORY/actions/runs/$RUN_ID/jobs?filter=latest" --jq '.jobs[]' | jq -s .)
# GitHub reports a job that never got a hosted runner as cancelled (or failed) with no steps.
unacquired='[]'
for id in $(jq -r '.[] | select((.conclusion == "failure" or .conclusion == "cancelled") and (.steps | length) == 0) | .id' <<< "$jobs"); do
  if gh api "repos/$REPOSITORY/check-runs/$id/annotations" --jq '.[].message' | grep -q 'was not acquired by Runner'; then
    unacquired=$(jq -c --argjson id "$id" '. + [$id]' <<< "$unacquired")
  fi
done
count=$(jq length <<< "$unacquired")
real=$(jq --argjson ids "$unacquired" '[.[] | select(.conclusion == "failure" and (.id | IN($ids[]) | not))] | length' <<< "$jobs")
echo "never acquired a runner: $count; other failed jobs: $real"
# Only an infrastructure-only failure is retried; any real failure stays red.
if [ "$count" -gt 0 ] && [ "$real" -eq 0 ]; then
  gh run rerun "$RUN_ID" --repo "$REPOSITORY" --failed
fi
'''


def install_unacquired_retry(root, workflow_dir, write):
    """Rerun, once, a GitHub run whose every failed job never got a hosted runner.

    GitHub-hosted macOS jobs sometimes fail with "The job was not acquired by
    Runner of type hosted even after multiple attempts" before any step starts.
    No repository code is checked out or executed; a failure with any started
    step, or a second attempt, is left alone.
    """
    names = []
    for path in sorted(workflow_dir.glob('*.y*ml')):
        if path.name == 'retry-unacquired-runners.yml':
            continue
        # Generated files start with a comment; the workflow name is a plain top-level scalar.
        match = re.search(r'^name:[ \t]*(.+?)[ \t]*$', path.read_text(), re.M)
        if match:
            names.append(match.group(1).strip('\'"'))
    if not names:
        return
    write('retry-unacquired-runners.yml', {
        'name': 'Retry unacquired runners',
        'on': {'workflow_run': {'workflows': sorted(set(names)), 'types': ['completed']}},
        'permissions': {'actions': 'write'},
        'jobs': {'retry': {
            'if': expr("github.event.workflow_run.conclusion == 'failure' && github.event.workflow_run.run_attempt == 1 && "
                       "github.event.workflow_run.repository.full_name == github.repository"),
            'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10,
            'steps': [{'name': 'Rerun only runner-acquisition failures',
                       'env': {'GH_TOKEN': expr('github.token'), 'REPOSITORY': expr('github.repository'),
                               'RUN_ID': expr('github.event.workflow_run.id')},
                       'run': UNACQUIRED_RETRY}]}}})


def install(args):
    import yaml
    yaml.SafeDumper.add_representer(str, multiline)
    root = args.directory.resolve()
    forge = args.forge
    github = forge == 'github'
    trusted_ref = trusted_branch_ref(forge, args.branch)
    config = json.loads(args.policy.read_text())
    image = config.get('forgejoDeterminateImage')
    labels = config.get('forgejoDeterminateLabels', [])
    if image is not None and (github or not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image)):
        raise SystemExit('forgejoDeterminateImage must be a pinned local image ID on Forgejo')
    if not isinstance(labels, list) or any(label not in ('nix', 'nix-light') for label in labels) or (labels and image is None):
        raise SystemExit('forgejoDeterminateLabels must list nix or nix-light and require an image')
    counterpart = config.get('githubCounterpartCi', False)
    if not isinstance(counterpart, bool) or (counterpart and github):
        raise SystemExit('githubCounterpartCi must be a boolean and applies only to Forgejo repositories')
    workflows = config.get('inputCheckWorkflows', [])
    if not isinstance(workflows, list) or any(not isinstance(w, str) or not re.fullmatch(r'[A-Za-z0-9_-]+[.]ya?ml', w) for w in workflows):
        raise SystemExit('inputCheckWorkflows must contain workflow filenames')
    config.update(version=1, forge=forge, repository=args.repository, branch=args.branch,
                  remote=('https://github.com/' if github else 'https://git.monke.fail/')+args.repository+'.git')
    ci = root / 'ci'
    ci.mkdir(exist_ok=True)
    for name in HELPERS:
        if ci / name != HERE / name:
            shutil.copy2(HERE / name, ci / name)
    for name in ['push-attic.py']:
        if not (ci / name).exists():
            shutil.copy2(HERE / name, ci / name)
    # Bytecode must never be tracked: running the helpers rewrites it.
    shutil.rmtree(ci / '__pycache__', ignore_errors=True)
    ignore = root / '.gitignore'
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    missing = [pattern for pattern in ('__pycache__/', '*.pyc') if pattern not in lines]
    if missing:
        ignore.write_text('\n'.join(lines + missing) + '\n')
    (ci / 'channels.json').write_text(json.dumps(config, indent=2)+'\n')
    hashes = {name: hashlib.sha256((ci/name).read_bytes()).hexdigest() for name in HELPERS}
    (ci / 'channel-tool-hashes.json').write_text(json.dumps(hashes, indent=2)+'\n')
    workflow_dir = root / ('.github/workflows' if github else '.forgejo/workflows')
    workflow_dir.mkdir(parents=True, exist_ok=True)
    if counterpart:
        # The GitHub counterpart already builds and checks every system; a
        # Forgejo native check would duplicate it. Remove any earlier copy.
        (workflow_dir / 'nix.yml').unlink(missing_ok=True)
    def write(name, value):
        (workflow_dir / name).write_text('# Generated by dave-fork-ci/ci/install-channel-automation.py\n'+yaml.safe_dump(value, sort_keys=False, width=110))
    def checkout(revision=None):
        if github:
            return [{'uses': CHECKOUT, 'with': {'ref': revision or expr('github.sha'), 'fetch-depth': 0,
                                              'submodules': 'recursive' if config.get('submodules') else False,
                                              'persist-credentials': False}}]
        return [{'name': 'Checkout exact source without a Node dependency',
                 'env': {'TOKEN': expr('github.token'), 'SERVER': expr('github.server_url'),
                         'REPO': expr('github.repository'), 'SHA': revision or expr('github.sha')},
                 'run': 'set -eu\nauth=$(printf "x-access-token:%s" "$TOKEN" | base64 | tr -d "\\n")\n'
                        'git init -q .\ngit remote add origin "$SERVER/$REPO.git"\n'
                        'git -c http.extraHeader="Authorization: Basic $auth" fetch -q origin "$SHA"\n'
                        'git checkout -q --detach FETCH_HEAD\n'}]
    def bootstrap(cores=1, runner=None):
        if not github:
            if not image or runner not in labels:
                return []
            return [{'name': 'Start isolated Determinate daemon', 'run':
                     "set -eu\nnix --version | grep -F 'Determinate Nix 3.24.1'\n"
                     "env -u NIX_REMOTE nix-daemon > /tmp/nix-daemon.log 2>&1 &\n"
                     "daemon=$!\nfor n in $(seq 1 30); do [ -S /nix/var/nix/daemon-socket/socket ] && break; kill -0 $daemon; sleep 1; done\n"
                     "test -S /nix/var/nix/daemon-socket/socket\n"
                     "test \"$(readlink -f /proc/$daemon/exe)\" = \"$(readlink -f \"$(command -v nix)\")\"\n"
                     "nix store info --store daemon --json\nprintf 'NIX_REMOTE=daemon\\n' >> \"$GITHUB_ENV\""}]

        intel = "runner.os == 'macOS' && runner.arch == 'X64'"
        common = 'accept-flake-config = false\nmax-jobs = 1\ncores = '+str(cores)+'\n'
        return [
            {'uses': INSTALL, 'if': expr('!('+intel+')'),
             'with': {'source-tag': 'v3.24.1', 'summarize': False, 'kvm': False,
                      'extra-conf': 'experimental-features = nix-command flakes parallel-eval\n'
                                    'eval-cores = 2\n'+common}},
            # Determinate no longer supports Intel macOS; preserve that native build.
            {'uses': INTEL_DARWIN_INSTALL, 'if': expr(intel),
             'with': {'nix_version': '2.34.7', 'enable_kvm': False,
                      'nix_conf': 'experimental-features = nix-command flakes\n'+common}},
            {'name': 'Check Determinate Nix and daemon', 'if': expr('!('+intel+')'),
             'run': "nix --version | grep -F 'Determinate Nix 3.24.1'\nnix store info --store daemon --json"},
        ]
    def job(runner=None, cache=False):
        default_runner = 'ubuntu-24.04' if github else 'nix'
        # Bytecode written into a checkout that tracks ci/__pycache__ makes the
        # clean-checkout checks fail, so no job ever writes it.
        result = {'runs-on': runner or default_runner, 'timeout-minutes': 360, 'env': {'PYTHONDONTWRITEBYTECODE': '1'}}
        if not github:
            result['env']['NIX_CONFIG'] = NIX_CONF_FORGEJO
        if not github and result['runs-on'] in {'nix', 'nix-light'}:
            result['container'] = {'image': 'nixos/nix@sha256:7a007c766426c1877758ddc5cb87a965ac131fc78c582ce0083d922d51ae945c'}
            if image and result['runs-on'] in labels:
                result['container'] = {'image': image}
                result['env']['NIX_REMOTE'] = 'daemon'
                result['env']['NIX_CONFIG'] += 'extra-experimental-features = parallel-eval\neval-cores = 2\n'
        return result
    def python(command):
        return ('python3 ' if github else 'nix shell --option accept-flake-config false --no-write-lock-file --inputs-from . nixpkgs#python3 -c python3 ') + command
    source_only = not (root / 'flake.nix').exists()
    if not config.get('targets'):
        previous = workflow_dir / 'release-channels.yml'
        if previous.exists() and previous.read_text().startswith('# Generated by dave-fork-ci/'):
            previous.unlink()
        if config.get('excludedSystems'):
            checks = job(None if github else 'nix-light')
            checks['steps'] = checkout()+bootstrap(runner=checks['runs-on'])+[{'name': 'Automation regression tests',
                'run': python('ci/test-release-channel.py')+'\n'+python('ci/test-push-niks3.py')+'\n'+python('ci/test-update-automation.py')+'\n'+python('ci/release-channel.py policy')}]
            write('automation-checks.yml', {'name': 'Automation checks', 'on': {'push': {'branches':[args.branch]},
                'pull_request':{}, 'workflow_dispatch':{}}, 'permissions':{'contents':'read'}, 'jobs':{'test':checks}})
    if config.get('targets'):
        test = job(None if github else 'nix-light')
        test['outputs'] = {'revision': expr('steps.selection.outputs.revision')}
        test['steps'] = checkout()+bootstrap(runner=test['runs-on'])+[
            {'name': 'Regression tests and complete inventory policy', 'run': python('ci/test-release-channel.py')+'\n'+python('ci/test-push-niks3.py')+'\n'+python('ci/test-update-automation.py')+'\n'+python('ci/release-channel.py policy')},
            {'name': 'Select immutable revision', 'id': 'selection',
             'env': {'REVISION': expr('inputs.revision || github.sha')},
             'run': python('-c \'import os,re; s=os.environ["REVISION"]; assert re.fullmatch("[a-f0-9]{40}",s); '
                           'open(os.environ["GITHUB_OUTPUT"],"a").write("revision="+s+"\\n")\'')}]
        jobs = {'test': test}
        proofs = {}
        needed = ['test']
        for system in config['targets']:
            key = 'build_' + system.replace('-', '_')
            native = job(({'x86_64-linux': 'ubuntu-24.04', 'aarch64-linux': 'ubuntu-24.04-arm',
                           'aarch64-darwin': 'macos-15', 'x86_64-darwin': 'macos-15-intel'} if github else
                          {'x86_64-linux': 'nix', 'aarch64-linux': 'braco',
                           'aarch64-darwin': 'darwin-aarch64', 'x86_64-darwin': 'darwin-x86_64'})[system], cache=True)
            native.update(needs=['test'], outputs={'proof': expr('steps.release.outputs.proof')},
                          **{'if': expr("github.event_name != 'pull_request' && " + trusted_ref)})
            # Keep derivations serial to bound memory, while allowing the
            # Linux builders to use every CPU assigned to their runners. The
            # macOS runners have 7 GB for 3 cores, so they stay at one core.
            native['steps'] = checkout(expr('needs.test.outputs.revision'))+bootstrap(
                cores=0 if system in {'x86_64-linux', 'aarch64-linux'} else 1, runner=native['runs-on'])+[
                {'name': 'Configure job-local private cache reader',
                 'env': {'ATTIC_TOKEN': expr('secrets.ATTIC_TOKEN || secrets.FLAKEY_ATTIC_TOKEN')},
                 'run': python('ci/configure-attic-read.py')},
                {'name': 'Build complete inventory, checkpoint closures, verify remote NAR bytes', 'id': 'release',
                 'env': {'ATTIC_TOKEN': expr('secrets.ATTIC_TOKEN || secrets.FLAKEY_ATTIC_TOKEN')},
                 'run': python('ci/release-channel.py build --system '+system)},
                {'name': 'Remove cache reader', 'if': expr('always()'),
                 'run': python('ci/configure-attic-read.py --cleanup')}]
            if github:
                native['permissions'] = {'contents': 'read', 'id-token': 'write'}
                for step in native['steps']:
                    if 'configure-attic-read.py' in step.get('run', ''):
                        step['run'] = step['run'].replace('configure-attic-read.py', 'configure-niks3.py')
                        if '--cleanup' not in step['run']:
                            step['run'] += ' --oidc'
                        step.pop('env', None)
                    elif 'release-channel.py build' in step.get('run', ''):
                        step.pop('env', None)
            else:
                for step in native['steps']:
                    if 'configure-attic-read.py' in step.get('run', ''):
                        step['run'] = step['run'].replace('configure-attic-read.py','configure-niks3.py')
                        if 'env' in step:
                            step['env'] = {'NIKS3_READ_NETRC': expr('secrets.FLAKEY_NIKS3_READ_NETRC'),
                                           'NIKS3_WRITE_TOKEN': expr('secrets.FLAKEY_NIKS3_WRITE_TOKEN')}
                    elif 'release-channel.py build' in step.get('run', ''):
                        step.pop('env', None)
            native['steps'].insert(-2, {'name': 'Install pinned Niks3 publisher', 'run': 'bash ci/install-niks3.sh'})
            if not github and system.endswith('-darwin'):
                # The host owns scheduling, power and capacity policy. Recheck
                # before tool bootstraps; the release helper also checks before
                # every package/check build in an already accepted job.
                for step in native['steps']:
                    if step.get('run', '').startswith('nix shell '):
                        step['run'] = '"$NATIVE_BUILD_ADMISSION_COMMAND"\n' + step['run']
                native['steps'][0]['run'] = ('test -n "${NATIVE_BUILD_ADMISSION_COMMAND:-}"\n'
                    '"$NATIVE_BUILD_ADMISSION_COMMAND"\n' + native['steps'][0]['run'])
            jobs[key] = native
            # Retention disables builders and only evaluates paths/reads NARs.
            # aarch64 Linux retention runs on the native braco runner (one job
            # at a time there); every other system stays on the icicle nix runner.
            retention = job('braco' if system == 'aarch64-linux' and not github else None, cache=True)
            retention['if'] = expr("(github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && " + trusted_ref)
            retention['steps'] = checkout()+bootstrap(runner=retention['runs-on'])+[
                {'name': 'Configure job-local private reader',
                 'env': {'ATTIC_TOKEN': expr('secrets.ATTIC_TOKEN || secrets.FLAKEY_ATTIC_TOKEN')},
                 'run': python('ci/configure-attic-read.py')},
                {'name': 'Verify advertised/recent releases independently of current builds',
                 'env': {'SOURCE_TOKEN': expr('github.token')},
                 'run': python('ci/release-channel.py retain --system '+system)},
                {'name': 'Remove private reader', 'if': expr('always()'),
                 'run': python('ci/configure-attic-read.py --cleanup')}]
            if github:
                retention['permissions'] = {'contents': 'read', 'id-token': 'write'}
                for step in retention['steps']:
                    if 'configure-attic-read.py' in step.get('run', ''):
                        step['run'] = step['run'].replace('configure-attic-read.py', 'configure-niks3.py')
                        if '--cleanup' not in step['run']:
                            step['run'] += ' --oidc'
                        step.pop('env', None)
            if not github:
                for step in retention['steps']:
                    if 'configure-attic-read.py' in step.get('run', ''):
                        step['run'] = step['run'].replace('configure-attic-read.py','configure-niks3.py')
                        if 'env' in step:
                            step['env'] = {'NIKS3_READ_NETRC': expr('secrets.FLAKEY_NIKS3_READ_NETRC')}
            jobs['retain_'+system.replace('-', '_')] = retention
            needed.append(key)
            proofs[system] = expr('needs.'+key+'.outputs.proof')
        promoter = job()
        promoter.update(needs=needed, permissions={'contents': 'write'},
                        **{'if': expr("github.event_name != 'pull_request' && " + trusted_ref)})
        promoter['steps'] = checkout(expr('needs.test.outputs.revision'))+bootstrap(runner=promoter['runs-on'])+[
            {'name': 'Atomically publish immutable release tag and channel',
             'env': {'CHANNEL_TOKEN': expr('secrets.CHANNEL_TOKEN || github.token'),
                     'RELEASE_PROOFS': json.dumps(proofs), 'REVISION': expr('needs.test.outputs.revision'),
                     'CHANNEL': expr("inputs.channel || 'tested'")},
             'run': python('ci/release-channel.py promote --channel "$CHANNEL" --revision "$REVISION" --proofs "$RELEASE_PROOFS"')}]
        jobs['promote'] = promoter
        triggers = {
            'push': {'branches': [args.branch]},
            'schedule': [{'cron': release_cron(args.repository)}],
            'workflow_dispatch': {'inputs': {'channel': {'type': 'choice', 'options': ['tested', 'stable'], 'default': 'tested'},
                                             'revision': {'type': 'string', 'description': 'Exact tested commit for stable; empty uses the event commit', 'required': False}}}}
        if counterpart:
            # GitHub checks pull requests. Pushes that only touch non-build
            # paths skip the private-cache build; the nightly run catches up.
            triggers['push']['paths-ignore'] = list(COUNTERPART_PATHS_IGNORE)
        write('release-channels.yml', {'name': 'Verified release channels', 'on': triggers,
            'permissions': {'contents': 'read'}, 'concurrency': {
                'group': 'verified-release-channels',
                'cancel-in-progress': False}, 'jobs': jobs})
        # PRs run the regression/policy test in a separate workflow. Forgejo
        # records a workflow with skipped release jobs as failed even when the
        # test job passed, so release-channels.yml must not trigger on PRs.
        pr_test = copy.deepcopy(test)
        pr_test['steps'][-1]['env']['REVISION'] = expr('github.sha')
        # On GitHub the bot-opened update and promotion branches never get pull_request
        # runs without approval, so the controller dispatches this same `test` job.
        write('pr-checks.yml', {'name': 'PR automation checks',
            'on': {'pull_request': {}, **({'workflow_dispatch': {}} if github else {})},
            'permissions': {'contents': 'read'}, 'jobs': {'test': pr_test}})
    monitor = job(None if github else 'nix-light')
    monitor['if'] = expr(trusted_ref)
    monitor['timeout-minutes'] = 120
    monitor['steps'] = checkout()+bootstrap(runner=monitor['runs-on'])+[
        {'name': 'Monitor manual package/file pins', 'id': 'monitor', 'env': {'GH_TOKEN': expr('github.token') if github else expr('secrets.GH_READ_TOKEN'),
                    'MONITOR_TOKEN': expr('github.token') if not github else ''},
         'run': python('ci/update-monitor.py')},
        {'name': 'Maintain one version monitoring issue', 'if': expr('always()'),
         'env': {'AUTOMATION_TOKEN': expr('secrets.UPDATE_TOKEN || github.token')},
         'run': python('ci/automation-forge.py report')}]
    jobs = {'monitor': monitor}
    if not source_only:
        updater = job(None if github else 'nix-light')
        updater['if'] = expr(trusted_ref)
        updater['timeout-minutes'] = 120
        updater['steps'] = checkout()+bootstrap(runner=updater['runs-on'])+[
            {'name': 'Resolve every tracked input and validate the exact candidate',
             'env': {'GH_TOKEN': expr('github.token') if github else expr('secrets.GH_READ_TOKEN')},
             'run': python('ci/update-inputs.py')},
            {'name': 'Open a review PR for changed pins', 'env': {'AUTOMATION_TOKEN': expr('secrets.UPDATE_TOKEN || github.token')},
             'run': python('ci/automation-forge.py inputs')}]
        jobs['inputs'] = updater
    write('scheduled-updates.yml', {'name': 'Input and manual version monitoring',
          'on': {'schedule': [{'cron': updates_cron(args.repository)}], 'workflow_dispatch': {}},
          'permissions': {'contents': 'write', 'issues': 'write', 'pull-requests': 'write', **({'actions': 'write'} if github else {})},
          'concurrency': {'group': 'scheduled-input-updates', 'cancel-in-progress': False}, 'jobs': jobs})
    if github:
        # Publisher binaries are used across steps and must survive runner GC.
        # Only touch their nix-build command, preserving native workflow layout.
        for path in workflow_dir.glob('*.y*ml'):
            text = path.read_text()
            text = re.sub(r'(client=\$\(nix build[^\n]*?)--no-link',
                          r'\1--out-link "${RUNNER_TEMP:-/tmp}/attic-client"', text)
            path.write_text(text)
        install_unacquired_retry(root, workflow_dir, write)
    if not github and not source_only:
        print('Next: grant the restricted updater account write access to ' + args.repository +
              ' (idempotent, needs an admin token): ci/ensure-updater-access.py --repository ' + args.repository)
    (ci / 'CHANNELS.md').write_text('''# Release and update automation

`main` (or the configured source branch) accepts development changes. `tested`
advances only after the entire explicit native inventory and flake checks build,
upload, and pass independent remote NAR content and signature verification.
Each target closure is policy checked and uploaded as soon as it builds. Valid
locally built or custom-cache outputs from its recursive derivation graph are
also published, including fixed-output/vendor dependencies and completed work
after a later build failure. Official-upstream-signed substitutes are filtered
using local metadata; this is not an independent upstream availability proof. These partial cache entries do not
advance a channel; promotion still waits for the full inventory, native checks,
and independent verification.
`stable` is an owner-dispatched selection of an immutable `tested-<commit>` tag;
it repeats builds/checks/cache verification. Flakes continue to lock exact commits.
Failed, skipped or missing platforms never advance a channel. Promotion uses
non-forced atomic pushes; release tags are immutable. No host activation occurs.
`excludedSystems` records intentional exclusions from that guarantee. Private
Darwin outputs use the on-prem native Forgejo runner and its immutable host
admission check before new package/check builds. An explicitly configured ARM
Mac may provide Intel Darwin compatibility only with Nix extra-platforms and
working Rosetta; other platform mismatches are rejected. Missing runner capacity
or an incomplete platform never counts as successful release/cache coverage.

The daily full run also verifies the advertised tested/stable revisions and the
three newest tags per channel against their original build proofs, without
source builds. These reads prove availability and integrity; they do not replace
the cache server's retention policy. Historical helper code is never executed.
No release/cache objects are deleted by this automation. Older tags remain
immutable source selections, but only this bounded recovery set is actively
rechecked; cache uptime is still monitored separately.

Every six hours, the updater resolves `tracking-inputs.json`, uses Nix to write
the lock, pins Git sources exactly in flake.nix, evaluates, and opens a review PR.
The manual-version monitor maintains one issue with candidates, failed checks,
and unclassified files. Dynamic/unrecognized sources require manual review.
This is monitoring, not blind rewriting of coordinated dependency hashes.

Required repository configuration:

- Enable Actions and permit the repository workflow token to create branches,
  tags, PRs and monitoring issues. Protect `tested`/`stable` against human or
  unrelated automation writes; allow only the release workflow identity.
- Forgejo requires repository-scoped `FLAKEY_NIKS3_READ_NETRC` and
  `FLAKEY_NIKS3_WRITE_TOKEN`. Niks3 is its substitute and verified release cache;
  existing Attic secrets and historical objects remain.
- GitHub requires no Niks3 static secrets: trusted publishers request a write grant through
  OIDC with audience `https://niks3.monke.fail`. Trusted readers use the separate
  read-only audience `https://niks3.monke.fail/read` and `/oidc` cache object route,
  refreshing the job-local netrc until cleanup. PR events receive no private
  reader. Niks3 upload and signed remote verification gate releases. No private
  Forgejo source credentials or inputs go to GitHub.
- `inputCheckWorkflows` lists reviewed GitHub native workflows with workflow_dispatch.
  The updater explicitly dispatches them on the immutable update branch, including
  when using GITHUB_TOKEN. Each workflow must keep cache publication and secrets
  restricted to the trusted source branch. The updater requires Actions write.
- Optional `UPDATE_TOKEN` is a repository-scoped app/PAT; default GITHUB_TOKEN
  PRs do not emit further pull_request workflow events.
- A new update candidate supersedes older marked input-only PRs for the same
  target branch after check dispatch succeeds. Branches are immutable and never
  force-pushed; candidates containing source fixes are preserved for review.
- Optional `CHANNEL_TOKEN` can identify a separate protected-branch publisher.
- Forgejo requires the existing native `nix` and native aarch64 Linux runners. Flakey
  and private inputs never go to GitHub. Already-public GitHub owning repos may
  use their existing native hosted Darwin builders independently.
- Forgejo jobs on the `nix` runner (x86_64 Linux build and the x86_64 Linux and
  Darwin retention jobs) set `ATTIC_SERVER` to the Attic host's runner-network
  address, so cache reads skip the public reverse proxy. The helpers accept only
  that address, the tailnet address of the same listener, or the public URL, send
  the token to one host only, and keep proofs independent of the transport. The
  braco aarch64 build and retention jobs, Darwin builds and all uploads use the
  public HTTPS URL. The aarch64 Linux retention job runs on braco, which executes
  one job at a time: it queues behind other repositories' aarch64 jobs.
- Schedules keep their cadence (release channels nightly, input updates every six
  hours) but start at a per-repository offset derived from a SHA-256 of
  `owner/name`: release channels at a minute of the UTC day (00:00-23:59), input updates at a
  minute in each six-hour slot starting at hour 0-5 UTC. The offset is stable, so
  repositories no longer fire in the same minute.
- On Forgejo, `UPDATE_TOKEN` belongs to the restricted `flakey-updater` account. A
  restricted account has no implicit repository access (the API answers 404), so
  the owner must grant it write access to this repository once, with an admin
  token: `ci/ensure-updater-access.py --repository OWNER/NAME` in dave-fork-ci.
- Forgejo may supply `GH_READ_TOKEN` for authenticated public metadata reads.
  Private input readers belong only on Forgejo; configure them before input jobs.

The helper source lives in dave-fork-ci/ci. `channel-tool-hashes.json` identifies
the installed bytes. Update the controller first, then regenerate/review copies
with `install-channel-automation.py`; do not silently download a moving script
into a credentialed publisher.
''' + (COUNTERPART_NOTE if counterpart else ''))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', required=True, type=Path)
    p.add_argument('--forge', required=True, choices=['github', 'forgejo'])
    p.add_argument('--repository', required=True)
    p.add_argument('--branch', required=True)
    p.add_argument('--policy', required=True, type=Path)
    install(p.parse_args())
