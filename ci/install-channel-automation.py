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
import runpy

CACHE_CONF = runpy.run_path(str(Path(__file__).with_name('cache-config.py')))['nix_config']()

def multiline(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)

HERE = Path(__file__).resolve().parent
CHECKOUT = 'actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803'
INSTALL = 'DeterminateSystems/determinate-nix-action@92957199488000b908fdad19f63711000965d8f3'
HOSTED_CLEANUP = 'wimpysworld/nothing-but-nix@c42920bb7515b766b1c69f5db46cb13124270f7e'
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
SHARED_DAEMON_IMAGE = 'sha256:2d46fea208b4e0a6440e121cca451e86e5c557926038af31f921f303d455cdcf'
ICICLE_LABELS = {'nix','nix-light','darwin-tools'}
HELPERS = ['caches.json', 'cache-config.py', 'release-channel.py', 'test-release-channel.py', 'update-inputs.py',
           'update-monitor.py', 'test-update-automation.py', 'automation-forge.py', 'configure-niks3.py', 'push-niks3.py', 'test-push-niks3.py', 'niks3-oidc-token.py', 'install-niks3.sh',
           'retain-build-outputs.py', 'test-retain-build-outputs.py', 'transfer-closures.py', 'test-transfer-closures.py']
# Attic-era helpers an older installer copied into consumers. Attic is retired;
# the installer removes these so a regenerated repository keeps no stale copy.
RETIRED_HELPERS = ['configure-attic-read.py', 'test-configure-attic-read.py', 'push-attic.py', 'test-push-attic.py',
                   'transfer-attic.py', 'test-transfer-attic.py']
# Installed only for repositories that opt in, so regenerating any other
# repository emits exactly its previous helper set and workflows.
SHARED_DAEMON_HELPERS = ['connect-shared-daemon.sh']
PRIVATE_DARWIN_HELPERS = ['tool-darwin.py', 'tool-darwin-evaluate.py', 'tool-darwin-protocol.py', 'tool-darwin-spool.py']


# Bounded Nix downloads: fail fast on a dead connection and retry transient DNS,
# cache and CDN errors instead of stalling a job for hours.
NIX_TRANSPORT_CONF = 'connect-timeout = 20\nstalled-download-timeout = 120\ndownload-attempts = 5\n'
NIX_CONF_FORGEJO = ('experimental-features = nix-command flakes\naccept-flake-config = false\n'
                    'sandbox = false\nfilter-syscalls = false\n' + NIX_TRANSPORT_CONF + CACHE_CONF)


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
    # Explicit opt-in to Icicle's one shared daemon/store. It needs the runner,
    # socket and volume layout from Flakey's shared-store cutover; without it
    # every job keeps its previous container, daemon and mounts exactly.
    shared = config.get('forgejoSharedDaemon', False)
    if not isinstance(shared, bool) or (shared and github):
        raise SystemExit('forgejoSharedDaemon must be a boolean and applies only to Forgejo repositories')
    allowed = ('nix', 'nix-light', 'darwin-tools') if shared else ('nix', 'nix-light')
    if not isinstance(labels, list) or any(label not in allowed for label in labels) or (labels and image is None):
        raise SystemExit('forgejoDeterminateLabels must list nix or nix-light and require an image')
    if shared and image not in (None, SHARED_DAEMON_IMAGE):
        # The connector pins the daemon's exact Nix; another image fails it.
        raise SystemExit('forgejoSharedDaemon jobs must use the shared daemon image')
    if shared and (root / 'ci/retained-archives.json').exists():
        # Reading a retained archive imports a Niks3-signed path into the
        # store, which the shared daemon's untrusted listener refuses.
        raise SystemExit('forgejoSharedDaemon cannot be combined with retained archives')
    if config.get('privateDarwin'):
        if github:
            raise SystemExit('privateDarwin currently admits Forgejo Darwin only')
        if not shared:
            raise SystemExit('privateDarwin requires the explicit forgejoSharedDaemon opt-in')
        route = config['privateDarwin']
        if (not route.get('attestorPublicKeys') or any(not re.fullmatch('[a-f0-9]{40}', route.get(k, '')) for k in ('clientCommit','providerCommit'))):
            raise SystemExit('privateDarwin requires reviewed attestors and executor pins')
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
    # A newly onboarded tool may have no custom monitor configuration yet.
    # The scanner still reports tracked pins and unclassified sources.
    monitor = ci / 'update-monitor.json'
    if not monitor.exists():
        monitor.write_text(json.dumps({'version': 1, 'checks': [], 'exclude': [], 'upstreams': []}, indent=2)+'\n')
    helpers = HELPERS + (SHARED_DAEMON_HELPERS if shared else []) + (PRIVATE_DARWIN_HELPERS if config.get('privateDarwin') else [])
    for name in helpers:
        if ci / name != HERE / name:
            shutil.copy2(HERE / name, ci / name)
    if not (ci / 'public-closure.py').exists():
        shutil.copy2(HERE / 'public-closure.py', ci / 'public-closure.py')
    for name in RETIRED_HELPERS:
        (ci / name).unlink(missing_ok=True)
    # Bytecode must never be tracked: running the helpers rewrites it.
    shutil.rmtree(ci / '__pycache__', ignore_errors=True)
    ignore = root / '.gitignore'
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    missing = [pattern for pattern in ('__pycache__/', '*.pyc') if pattern not in lines]
    if missing:
        ignore.write_text('\n'.join(lines + missing) + '\n')
    (ci / 'channels.json').write_text(json.dumps(config, indent=2)+'\n')
    hashes = {name: hashlib.sha256((ci/name).read_bytes()).hexdigest() for name in helpers}
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
    def bootstrap(cores=1, runner=None, cleanup=False):
        if not github:
            if shared:
                if runner not in ICICLE_LABELS:
                    return []
                return [{'name':'Connect to the shared Icicle Nix daemon',
                         'run':'bash ci/connect-shared-daemon.sh'}]
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
        common = 'accept-flake-config = false\nmax-jobs = 1\ncores = '+str(cores)+'\n' + CACHE_CONF
        cleanup_steps = []
        if cleanup:
            cleanup_steps = [{'name': 'Reclaim hosted runner space for Nix', 'uses': HOSTED_CLEANUP,
                              'if': expr('!('+intel+')'),
                              'with': {'hatchet-protocol': 'cleave',
                                       'root-safe-haven': expr("runner.os == 'Linux' && '16384' || 'auto'"),
                                       'witness-carnage': True}}]
            if runner and runner.startswith('ubuntu-'):
                common += 'build-dir = /nix/build\n'
        return cleanup_steps + [
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
        if not github and result['runs-on'] in {'nix', 'nix-light', 'darwin-tools'}:
            result['container'] = {'image': 'nixos/nix@sha256:7a007c766426c1877758ddc5cb87a965ac131fc78c582ce0083d922d51ae945c'}
            if shared and result['runs-on'] in ICICLE_LABELS:
                # Tool jobs are untrusted clients of the one shared store:
                # read-only aliases of it and of the untrusted listener's
                # socket directory (so no job can replace that socket), never
                # the store or socket volumes, trusted socket or creds tmpfs.
                result['container'] = {'image': image or SHARED_DAEMON_IMAGE, 'volumes': [
                    'flakey-ci-nix-pr-v2-ro:/nix:ro',
                    'flakey-ci-nix-pr-socket-untrusted-ro:/nix/var/nix/daemon-socket:ro']}
                result['env']['NIX_REMOTE'] = 'daemon'
                result['env']['NIX_SHARED_DAEMON'] = '1'
                result['env']['NIX_CONFIG'] += 'extra-experimental-features = parallel-eval\neval-cores = 2\n'
            elif image and result['runs-on'] in labels:
                result['container'] = {'image': image}
                result['env']['NIX_REMOTE'] = 'daemon'
                result['env']['NIX_CONFIG'] += 'extra-experimental-features = parallel-eval\neval-cores = 2\n'
        return result
    def shell(command, readonly=False):
        if github:
            return command
        flags = '--option max-jobs 0 --option builders "" --option allow-import-from-derivation false ' if readonly else ''
        return 'nix shell '+flags+'--option accept-flake-config false --no-write-lock-file --inputs-from . nixpkgs#python3 -c '+command
    def python(command, readonly=False):
        return shell('python3 '+command, readonly=readonly)
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
        manifests = {}
        darwin_proofs = {}
        needed = ['test']
        for system in config['targets']:
            key = 'build_' + system.replace('-', '_')
            native = job(({'x86_64-linux': 'ubuntu-24.04', 'aarch64-linux': 'ubuntu-24.04-arm',
                           'aarch64-darwin': 'macos-15', 'x86_64-darwin': 'macos-15-intel'} if github else
                          {'x86_64-linux': 'nix', 'aarch64-linux': 'braco',
                           'aarch64-darwin': 'darwin-tools' if config.get('privateDarwin') else 'darwin-aarch64', 'x86_64-darwin': 'darwin-tools' if config.get('privateDarwin') else 'darwin-x86_64'})[system], cache=True)
            native.update(needs=['test'], outputs={'proof': expr('steps.release.outputs.proof'),
                                                   'retention': expr('steps.release.outputs.retention')},
                          **{'if': expr("github.event_name != 'pull_request' && " + trusted_ref)})
            # Keep derivations serial to bound memory, while allowing the
            # Linux builders to use every CPU assigned to their runners. The
            # macOS runners have 7 GB for 3 cores, so they stay at one core.
            native['steps'] = checkout(expr('needs.test.outputs.revision'))+bootstrap(
                cores=0 if system in {'x86_64-linux', 'aarch64-linux'} else 1, runner=native['runs-on'], cleanup=True)+[
                # GitHub reads and publishes through OIDC; Forgejo uses the
                # repository-scoped Niks3 netrc and write token.
                {'name': 'Configure job-local private cache reader',
                 **({} if github else {'env': {'NIKS3_READ_NETRC': expr('secrets.FLAKEY_NIKS3_READ_NETRC'),
                                               'NIKS3_WRITE_TOKEN': expr('secrets.FLAKEY_NIKS3_WRITE_TOKEN')}}),
                 'run': python('ci/configure-niks3.py' + (' --oidc' if github else ''))},
                {'name': 'Build complete inventory, checkpoint closures, verify signed cache metadata, pin release', 'id': 'release',
                 # GitHub: one live progress check run. Forgejo: read-only
                 # ledger access for the retention pins.
                 'env': {'STATUS_TOKEN' if github else 'SOURCE_TOKEN': expr('github.token')},
                 'run': python('ci/release-channel.py build --system '+system)},
                {'name': 'Remove cache reader', 'if': expr('always()'),
                 'run': python('ci/configure-niks3.py --cleanup', readonly=True)}]
            if github:
                # checks: write lets the gate keep one live progress check run;
                # GitHub serves job logs only after a job ends.
                native['permissions'] = {'contents': 'read', 'id-token': 'write', 'checks': 'write'}
            native['steps'][-2:-2] = [
                {'name': 'Install pinned Niks3 publisher and retain its build dependencies',
                 'run': shell('bash ci/install-niks3.sh --retain-bootstrap')},
                {'name': 'Publish bootstrap client and completed build dependencies',
                 'run': python('ci/push-niks3.py --outputs niks3-bootstrap-outputs.txt', readonly=True)}]
            if not github and system.endswith('-darwin') and not config.get('privateDarwin'):
                # The host owns scheduling, power and capacity policy. Recheck
                # before tool bootstraps; the release helper also checks before
                # every package/check build in an already accepted job.
                for step in native['steps']:
                    if step.get('run', '').startswith('nix shell ') and '--option max-jobs 0 --option builders ""' not in step['run']:
                        step['run'] = '"$NATIVE_BUILD_ADMISSION_COMMAND"\n' + step['run']
                native['steps'][0]['run'] = ('test -n "${NATIVE_BUILD_ADMISSION_COMMAND:-}"\n'
                    '"$NATIVE_BUILD_ADMISSION_COMMAND"\n' + native['steps'][0]['run'])
            if not github and config.get('privateDarwin') and system.endswith('-darwin'):
                native['timeout-minutes'] = 360
                native['container']['volumes'] += ['flakey-darwin-tool-'+args.repository.split('/')[1]+'-metadata:/run/darwin-tool-metadata']
                native['outputs']['darwin'] = expr('steps.release.outputs.darwin')
                darwin_proofs[system] = expr('needs.'+key+'.outputs.darwin')
                for step in native['steps']:
                    if 'release-channel.py build' in step.get('run', ''):
                        step['env']['PRIVATE_DARWIN_HMAC_KEY'] = expr('secrets.PRIVATE_DARWIN_HMAC_KEY')
            jobs[key] = native
            # Retention disables builders and only evaluates paths/reads NARs.
            # aarch64 Linux retention runs on the native braco runner (one job
            # at a time there); every other system stays on the icicle nix runner.
            retention = job('braco' if system == 'aarch64-linux' and not github else None, cache=True)
            retention['if'] = expr("(github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && " + trusted_ref)
            retention['steps'] = checkout()+bootstrap(runner=retention['runs-on'])+[
                {'name': 'Configure job-local private reader',
                 **({} if github else {'env': {'NIKS3_READ_NETRC': expr('secrets.FLAKEY_NIKS3_READ_NETRC')}}),
                 'run': python('ci/configure-niks3.py' + (' --oidc' if github else ''))},
                {'name': 'Verify advertised/recent releases independently of current builds',
                 'env': {'SOURCE_TOKEN': expr('github.token')},
                 'run': python('ci/release-channel.py retain --system '+system)},
                {'name': 'Remove private reader', 'if': expr('always()'),
                 'run': python('ci/configure-niks3.py --cleanup', readonly=True)}]
            if github:
                retention['permissions'] = {'contents': 'read', 'id-token': 'write'}
            jobs['retain_'+system.replace('-', '_')] = retention
            needed.append(key)
            proofs[system] = expr('needs.'+key+'.outputs.proof')
            manifests[system] = expr('needs.'+key+'.outputs.retention')
        promoter = job()
        promoter.update(needs=needed, permissions={'contents': 'write'},
                        **{'if': expr("github.event_name != 'pull_request' && " + trusted_ref)})
        promoter['steps'] = checkout(expr('needs.test.outputs.revision'))+bootstrap(runner=promoter['runs-on'])+[
            {'name': 'Atomically publish immutable release tag and channel',
             'env': {'CHANNEL_TOKEN': expr('secrets.CHANNEL_TOKEN || github.token'),
                     'RELEASE_PROOFS': json.dumps(proofs), 'RELEASE_RETENTION': json.dumps(manifests),
                     'REVISION': expr('needs.test.outputs.revision'),
                     'CHANNEL': expr("inputs.channel || 'tested'")},
             'run': python('ci/release-channel.py promote --channel "$CHANNEL" --revision "$REVISION" --proofs "$RELEASE_PROOFS" --retention "$RELEASE_RETENTION"')}]
        if config.get('privateDarwin'):
            proof_script = 'mkdir -p /tmp/darwin-release-proofs\n'
            for proof_system, expression in darwin_proofs.items():
                # toJSON makes the substitution a JSON string; the heredoc is
                # inert base64 data generated by the trusted build helper.
                proof_script += ("cat > /tmp/darwin-release-proofs/"+proof_system+".json <<'DARWIN_PROOF'\n"
                                 +expr('toJSON('+expression.removeprefix('${{ ').removesuffix(' }}')+')')+"\nDARWIN_PROOF\n")
            promoter['steps'].insert(-1, {'name':'Materialize signed Darwin proof files','run':proof_script})
            promote_step=promoter['steps'][-1]
            promote_step['env']['PRIVATE_DARWIN_PROOF_DIRECTORY']='/tmp/darwin-release-proofs'
            promoter['steps'].insert(-1, {'name':'Configure cache reader for signed worker closure binding',
                'env':{'NIKS3_READ_NETRC':expr('secrets.FLAKEY_NIKS3_READ_NETRC')},
                'run':python('ci/configure-niks3.py')})
            promoter['steps'].append({'name':'Remove cache reader', 'if':expr('always()'),
                'run':python('ci/configure-niks3.py --cleanup', readonly=True)})
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
        {'name': 'Maintain one version monitoring issue',
         'if': expr('always() && github.event.repository.has_issues' if github else 'always()'),
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
        if github:
            updater['permissions'] = {'contents': 'write', 'pull-requests': 'write',
                                      'actions': 'write', 'statuses': 'write'}
            updater['steps'][-1]['env']['AUTOMATION_STATUS_TOKEN'] = expr('github.token')
        jobs['inputs'] = updater
    write('scheduled-updates.yml', {'name': 'Input and manual version monitoring',
          'on': {'schedule': [{'cron': updates_cron(args.repository)}], 'workflow_dispatch': {}},
          'permissions': {'contents': 'write', 'issues': 'write', 'pull-requests': 'write', **({'actions': 'write'} if github else {})},
          'concurrency': {'group': 'scheduled-input-updates', 'cancel-in-progress': False}, 'jobs': jobs})
    if github:
        install_unacquired_retry(root, workflow_dir, write)
    if not github and not source_only:
        print('Next: grant the restricted updater account write access to ' + args.repository +
              ' (idempotent, needs an admin token): ci/ensure-updater-access.py --repository ' + args.repository)
    (ci / 'CHANNELS.md').write_text('''# Release and update automation

`main` (or the configured source branch) accepts development changes. `tested`
advances only after the entire explicit native inventory and flake checks build
and upload, and every path of the built closures passes the cache metadata gate:
Niks3 serves a narinfo for each root with a valid Niks3 signature, every other
path is served by Niks3 with the Niks3 signature or, when Niks3 omitted it, by
cache.nixos.org with the official signature, and narHash, narSize and references
equal the build (the signature binds them). Lookups are parallel and batched;
no NAR bytes are read before promotion, so a missing or corrupt NAR object
behind a valid signed narinfo is not caught here. The daily audit below reads
those bytes, and downstream deployments that substitute the closure are the
end-to-end proof.
Each target closure is policy checked and uploaded as soon as it builds. Valid
locally built or custom-cache outputs from its recursive derivation graph are
also published, including fixed-output/vendor dependencies and completed work
after a later build failure. Official-upstream-signed substitutes are filtered
using local metadata; this is not an independent upstream availability proof. These partial cache entries do not
advance a channel; promotion still waits for the full inventory, native checks,
and the metadata gate.
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

Niks3 garbage-collects closures that were not pushed within its retention
window unless a pin names them. Each build writes a small `release-roots`
manifest per system (a store path whose references are exactly the release
roots), pushes it pinned as `<owner>-<name>.<system>.candidate`, and the
release tag records it under `retention`. Before that, the build re-pins the
advertised `tested`/`stable` releases and the three newest tags per channel
under fixed role names (`.tested`, `.stable`, `.tested-recent-1` ...). Pins
are only created or moved, never deleted; a moved pin returns its previous
closure to the normal push-age window. A pin failure fails the build, so the
channel does not advance. Releases tagged before manifests existed are kept
only by the push-age window until they leave the recovery set.

The daily full run also verifies the advertised tested/stable revisions and the
three newest tags per channel against their original build proofs, without
source builds. It reads and hashes every NAR, so it is the content audit; it
does not gate promotion. Historical helper code is never executed. No release
or cache objects are deleted by this automation. Older tags remain immutable
source selections, but only this bounded recovery set is pinned and actively
rechecked; cache uptime is still monitored separately.

Every six hours, the updater resolves `tracking-inputs.json`, uses Nix to write
the lock, pins Git sources exactly in flake.nix and evaluates. Changed pins go
to one `automation/input-updates` branch and PR per repository, updated in place.
On Forgejo that PR squash-merges itself once the required checks pass
(`"inputAutoMerge": false` in channels.json turns this off).
The manual-version monitor maintains one issue with candidates, failed checks,
and unclassified files. Dynamic/unrecognized sources require manual review.
This is monitoring, not blind rewriting of coordinated dependency hashes.

Required repository configuration:

- Enable Actions and permit the repository workflow token to create branches,
  tags, PRs and monitoring issues. Protect `tested`/`stable` against human or
  unrelated automation writes; allow only the release workflow identity.
- Forgejo requires repository-scoped `FLAKEY_NIKS3_READ_NETRC` and
  `FLAKEY_NIKS3_WRITE_TOKEN`. Niks3 is its substitute and verified release cache.
- GitHub requires no Niks3 static secrets: trusted publishers request a write grant through
  OIDC with audience `https://niks3.monke.fail`. Trusted readers use the separate
  read-only audience `https://niks3.monke.fail/read` and `/oidc` cache object route,
  refreshing the job-local netrc until cleanup. PR events receive no private
  reader. Niks3 upload and signed remote metadata gate releases. No private
  Forgejo source credentials or inputs go to GitHub.
- GitHub release build jobs hold `checks: write` and keep one check run per
  system (`release progress / <system>`) updated with the current target, check
  count or phase, because GitHub serves job logs only after a job ends. Follow
  it with `gh api repos/OWNER/NAME/commits/SHA/check-runs --jq
  '.check_runs[] | select(.name | startswith("release progress")) | [.name, .status, .output.title]'`.
  The token never reaches Nix; a killed job leaves its check run in progress.
- Native flake checks whose outputs are already built inventory roots (for
  example `checks = self.packages // { ... }`) are not built a second time; only
  the remaining checks run.
- Retention pins use the same publisher grant (write scope; creating or moving a
  pin needs no admin rights). The build reads the release ledger read-only: the
  job token on Forgejo, anonymously for public GitHub repositories. Pin names are
  not reserved by default, so any publisher can move them; the Niks3 server can
  reserve each repository's `<owner>-<name>.*` prefix for that repository.
- `inputCheckWorkflows` lists reviewed GitHub native workflows with workflow_dispatch.
  The updater explicitly dispatches them on the update branch head, including
  when using GITHUB_TOKEN. Each workflow must keep cache publication and secrets
  restricted to the trusted source branch. The updater requires Actions write.
  It waits for the dispatched PR checks workflow’s `test` job on the exact head
  and publishes its success with the GitHub Actions token and Statuses write.
- Optional `UPDATE_TOKEN` is a repository-scoped app/PAT. With GITHUB_TOKEN,
  GitHub requires approval before the pull_request workflow runs.
- Each update is a commit of the current target branch plus the new pins, with
  the previous update head as a parent, so the branch only moves forward and is
  never force-pushed. A candidate whose checks are still pending is left to
  finish for up to 12 hours. Older hash-named input-only PRs are closed;
  candidates containing source fixes are preserved for review.
- Optional `CHANNEL_TOKEN` can identify a separate protected-branch publisher.
- Forgejo requires the existing native `nix` and native aarch64 Linux runners. Flakey
  and private inputs never go to GitHub. Already-public GitHub owning repos may
  use their existing native hosted Darwin builders independently.
- Every job reads and publishes through the public HTTPS Niks3 URL. The aarch64
  Linux retention job runs on braco, which executes one job at a time: it queues
  behind other repositories' aarch64 jobs.
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
