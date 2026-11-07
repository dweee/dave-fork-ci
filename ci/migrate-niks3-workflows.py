#!/usr/bin/env python3
"""Migrate reviewed native GitHub cache workflows to OIDC-backed Niks3.

Requires PyYAML. Changes are planned for the whole directory before writing;
unknown credential-bearing steps require manual repair rather than a guess.
The channel installer owns release-channels.yml, which this tool never edits.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import shlex
import sys


KEY = 'niks3.monke.fail-1:q13GqiZgAxx7SyoLZInXoJjviZMOdNpfG03xs0VUD2M='
OLD_KEY = 'flakey:mDx4DiNpH5w1Pl8OAKFFWq1ZsypA9lbaFKNiRWhqYE8='
CREDENTIAL_KEYS = {
    'ATTIC_TOKEN', 'FLAKEY_ATTIC_TOKEN', 'ATTIC_PULL_TOKEN', 'ATTIC_APPROVED_PR_SHA',
    'ATTIC_BIN', 'ATTIC_ENDPOINT', 'ATTIC_CACHE', 'ATTIC_SERVER',
    'ATTIC_SUBSTITUTER', 'ATTIC_PUBLIC_KEY',
}
CACHE_FLAG = '(vars.NIKS3_PUSH_ENABLED || vars.ATTIC_PUSH_ENABLED)'
CALLER = 'dweee/dave-fork-ci/.github/workflows/upstream-sync.yml@'


class MigrationError(ValueError):
    """A workflow cannot be migrated without understanding additional code."""


def yaml_types():
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    # GitHub uses YAML 1.2. In particular on/off/yes/no are strings, old-style
    # octal and sexagesimal numbers are not YAML 1.2 integers, and dates are not
    # implicitly converted to Python objects. Never alter PyYAML's global loader.
    removed = {'tag:yaml.org,2002:' + kind for kind in ('bool', 'int', 'float', 'timestamp')}
    Loader.yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in values if tag not in removed]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))
    Loader.add_implicit_resolver('tag:yaml.org,2002:int', re.compile(r'^[-+]?(?:0o[0-7]+|0x[0-9a-fA-F]+|[0-9]+)$'), list('-+0123456789'))
    Loader.add_implicit_resolver('tag:yaml.org,2002:float', re.compile(r'^(?:[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[-+]?[0-9]+[eE][-+]?[0-9]+|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$'), list('-+0123456789.'))

    def integer(loader, node):
        value = loader.construct_scalar(node)
        sign = -1 if value.startswith('-') else 1
        value = value.lstrip('+-')
        return sign * int(value[2:] if value.startswith(('0o', '0x')) else value,
                          8 if value.startswith('0o') else 16 if value.startswith('0x') else 10)

    def mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise MigrationError('Duplicate YAML mapping key: ' + str(key))
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    Loader.add_constructor('tag:yaml.org,2002:int', integer)
    Loader.add_constructor('tag:yaml.org,2002:map', mapping)

    class Dumper(yaml.SafeDumper):
        def ignore_aliases(self, value):
            return True

    Dumper.add_representer(str, lambda dumper, value: dumper.represent_scalar(
        'tag:yaml.org,2002:str', value, style='|' if '\n' in value else None))
    return yaml, Loader, Dumper


def loads(text):
    yaml, loader, _ = yaml_types()
    try:
        return yaml.load(text, Loader=loader)
    except yaml.YAMLError as error:
        raise MigrationError('Invalid workflow YAML: ' + str(error)) from error


def dumps(value):
    yaml, _, dumper = yaml_types()
    return yaml.dump(value, Dumper=dumper, sort_keys=False, width=100000, allow_unicode=True)


def command(run, script):
    """Accept one plain Python invocation, never a mixed shell program."""
    if not isinstance(run, str) or '\n' in run.strip():
        return None
    try:
        words = shlex.split(run.strip())
    except ValueError:
        return None
    if len(words) >= 2 and words[:2] == ['python3', 'ci/' + script]:
        return words[2:]
    return None


def manifest_args(args):
    return args == [] or (len(args) == 2 and args[0] == '--outputs'
                          and re.fullmatch(r'[A-Za-z0-9_./-]+', args[1])
                          and '..' not in Path(args[1]).parts)


def attic_install(run):
    """Recognize the existing trusted-client install, including rooted variants."""
    lines = [line.strip() for line in run.replace('\\\n', ' ').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if lines and lines[0] == 'set -euo pipefail':
        lines.pop(0)
    if not lines or not lines[0].startswith('client=$(nix build ') or not lines[0].endswith(')'):
        return False
    invocation = lines.pop(0)[len('client=$('):-1]
    if '$(' in invocation or '`' in invocation:
        return False
    try:
        words = shlex.split(invocation)
    except ValueError:
        return False
    if words[:2] != ['nix', 'build']:
        return False
    words = words[2:]
    found = False
    while words:
        word = words.pop(0)
        if re.fullmatch(r'(?:nixpkgs|github:NixOS/nixpkgs/[a-f0-9]{40})#attic-client', word):
            if found:
                return False
            found = True
        elif word in {'--no-update-lock-file', '--no-write-lock-file', '--no-link', '--print-out-paths'}:
            continue
        elif word in {'--inputs-from', '--max-jobs', '--builders', '--out-link'}:
            if not words:
                return False
            value = words.pop(0)
            if word == '--inputs-from' and value != '.':
                return False
            if word == '--max-jobs' and value != '0':
                return False
            if word == '--builders' and value != '':
                return False
            if word == '--out-link' and value not in {'$RUNNER_TEMP/attic-client', '${RUNNER_TEMP}/attic-client', '${RUNNER_TEMP:-/tmp}/attic-client'}:
                return False
        elif word == '--option':
            if len(words) < 2 or tuple(words[:2]) not in {
                    ('accept-flake-config', 'false'), ('substituters', 'https://cache.nixos.org')}:
                return False
            del words[:2]
        else:
            return False
    if lines and lines[0] == 'test -x "$client/bin/attic"':
        lines.pop(0)
    return found and lines in [
        ['echo "binary=$client/bin/attic" >> "$GITHUB_OUTPUT"'],
        ['echo "ATTIC_BIN=$client/bin/attic" >> "$GITHUB_ENV"'],
    ]


def permissions(document, job):
    existing = job.get('permissions', document.get('permissions', {'contents': 'read'}))
    if not isinstance(existing, dict):
        raise MigrationError('Review scalar permissions before adding id-token: write')
    job['permissions'] = dict(existing) | {'id-token': 'write'}


def trusted_gate(job, step):
    gate = str(job.get('if', '')) + ' ' + str(step.get('if', ''))
    return ('github.ref' in gate and ('github.event.repository.default_branch' in gate or 'refs/heads/' in gate)
            and any("'" + event + "'" in gate or '"' + event + '"' in gate
                    for event in ('push', 'workflow_dispatch', 'schedule')))


def clean_environment(step):
    env = step.get('env', {})
    if not isinstance(env, dict):
        raise MigrationError('Review non-mapping step environment')
    unknown = {key for key in env if str(key).startswith('ATTIC_') and key not in CREDENTIAL_KEYS}
    if unknown:
        raise MigrationError('Unrecognized cache environment keys: ' + ', '.join(sorted(unknown)))
    env = {key: value for key, value in env.items() if key not in CREDENTIAL_KEYS}
    if env:
        step['env'] = env
    else:
        step.pop('env', None)


def cache_flags(value, field=None):
    if isinstance(value, str):
        if field not in {'if', 'auto-merge', 'cache'} or 'vars.NIKS3_PUSH_ENABLED' in value:
            return value
        return re.sub(r'\bvars\.ATTIC_PUSH_ENABLED\b', CACHE_FLAG, value)
    if isinstance(value, dict):
        return {key: cache_flags(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [cache_flags(item, field) for item in value]
    return value


def migrate_document(document, controller_revision=None):
    if controller_revision is not None and not re.fullmatch(r'[a-f0-9]{40}', controller_revision):
        raise MigrationError('--controller-revision must be an exact lowercase 40-character commit')
    if not isinstance(document, dict) or not isinstance(document.get('jobs'), dict):
        raise MigrationError('Expected a workflow mapping with jobs')
    document = copy.deepcopy(document)
    manual = []
    for job_name, job in document['jobs'].items():
        if not isinstance(job, dict):
            raise MigrationError('Expected job mapping: ' + str(job_name))
        uses = job.get('uses', '')
        if isinstance(uses, str) and uses.startswith(CALLER):
            if controller_revision:
                job['uses'] = CALLER + controller_revision
                settings = job.setdefault('with', {})
                if not isinstance(settings, dict) or settings.get('tool-repository', 'dweee/dave-fork-ci') != 'dweee/dave-fork-ci':
                    raise MigrationError('Unexpected shared controller repository')
                settings.update({'tool-repository': 'dweee/dave-fork-ci', 'tool-revision': controller_revision})
            settings = job.get('with', {})
            for key in list(settings):
                if key.startswith('attic-'):
                    del settings[key]
            secrets = job.get('secrets', {})
            if not isinstance(secrets, dict):
                raise MigrationError('Review inherited secrets on shared controller caller')
            secrets.pop('ATTIC_TOKEN', None)
            if 'secrets' in job and not secrets:
                del job['secrets']
            permissions(document, job)
        steps = job.get('steps', [])
        if not isinstance(steps, list):
            raise MigrationError('Expected step list: ' + str(job_name))
        rewritten, removed_ids, has_reader, has_publisher = [], [], False, False
        for original in steps:
            if not isinstance(original, dict):
                raise MigrationError('Expected step mapping in ' + str(job_name))
            step = copy.deepcopy(original)
            run = step.get('run', '')
            if not isinstance(run, str):
                raise MigrationError('Expected string run command')
            args = command(run, 'configure-attic-read.py')
            if args is not None:
                if args == ['--pr-read']:
                    if 'ATTIC_APPROVED_PR_SHA' not in str(step.get('if', '')):
                        raise MigrationError('Unrecognized approved-PR private reader guard')
                    if step.get('id'):
                        removed_ids.append(step['id'])
                    continue
                if args not in ([], ['--cleanup']):
                    raise MigrationError('Unrecognized Attic reader invocation')
                clean_environment(step)
                if args == []:
                    if not trusted_gate(job, step):
                        raise MigrationError('Private reader lacks a trusted ref/event guard: ' + str(job_name))
                    step['name'] = 'Configure read-only Niks3 access with GitHub OIDC'
                    step['run'] = 'python3 ci/configure-niks3.py --oidc'
                    has_reader = True
                else:
                    step['run'] = 'python3 ci/configure-niks3.py --cleanup'
            elif 'ci/configure-attic-read.py' in run:
                raise MigrationError('Mixed or unsupported Attic reader command: ' + str(job_name))
            elif 'attic-client' in run:
                if not attic_install(run):
                    raise MigrationError('Mixed or unrecognized trusted Attic client install: ' + str(job_name))
                if step.get('id'):
                    removed_ids.append(step['id'])
                continue
            elif command(run, 'push-attic.py') is not None:
                args = command(run, 'push-attic.py')
                if not manifest_args(args) or not trusted_gate(job, step):
                    raise MigrationError('Unsupported manifest or untrusted publisher guard: ' + str(job_name))
                clean_environment(step)
                matching = [item for item in steps if isinstance(item, dict)
                            and command(item.get('run', ''), 'push-niks3.py') == args]
                if matching:
                    if len(matching) != 1 or matching[0].get('if') != step.get('if'):
                        raise MigrationError('Niks3/Attic publication guards do not match')
                    if step.get('id'):
                        removed_ids.append(step['id'])
                    continue
                step['name'] = 'Publish to Niks3 with GitHub OIDC and verify S3 integrity'
                step['run'] = 'python3 ci/push-niks3.py' + (' --outputs ' + args[1] if args else '')
                has_publisher = True
            elif 'ci/push-attic.py' in run:
                raise MigrationError('Mixed or unsupported Attic publisher command: ' + str(job_name))
            elif command(run, 'configure-niks3.py') == ['--oidc']:
                if not trusted_gate(job, step):
                    raise MigrationError('OIDC reader lacks a trusted ref/event guard: ' + str(job_name))
                has_reader = True
            elif 'ci/configure-niks3.py' in run and command(run, 'configure-niks3.py') != ['--cleanup']:
                raise MigrationError('Mixed or unsupported Niks3 reader command: ' + str(job_name))
            elif command(run, 'push-niks3.py') is not None:
                args = command(run, 'push-niks3.py')
                if not manifest_args(args) or not trusted_gate(job, step):
                    raise MigrationError('Unsupported or untrusted Niks3 publisher: ' + str(job_name))
                has_publisher = True
            elif run.strip() in {'rm -f "$RUNNER_TEMP/flakey-attic.netrc"', 'rm -f $RUNNER_TEMP/flakey-attic.netrc'}:
                step['run'] = 'python3 ci/configure-niks3.py --cleanup'
            elif re.search(r'\battic\s+(?:login|use|push)\b|\$\{?ATTIC_(?:BIN|TOKEN)\b', run):
                raise MigrationError('Unsupported inline Attic credential/publisher command: ' + str(job_name))
            if 'ci/build-cache-only.py' in run:
                manual.append(str(job_name) + ': review ci/build-cache-only.py publication manually; script was not edited')
            env = step.get('env', {})
            if isinstance(env, dict) and CREDENTIAL_KEYS.intersection(env):
                if 'ci/build-cache-only.py' in run:
                    raise MigrationError('ci/build-cache-only.py requires manual credential/publication repair: ' + str(job_name))
                raise MigrationError('Unrecognized credential-bearing step requires manual repair: ' + str(job_name) + ': ' + str(step.get('name', run)))
            for field in ('extra-conf', 'nix_conf'):
                settings = step.get('with', {})
                if not isinstance(settings, dict) or not isinstance(settings.get(field), str):
                    continue
                config = settings[field]
                config = config.replace(OLD_KEY, KEY)
                for variable, setting in [('ATTIC_SUBSTITUTER', 'extra-substituters'), ('ATTIC_PUBLIC_KEY', 'extra-trusted-public-keys')]:
                    pattern = r'(?m)^([ \t]*' + setting + r'[ \t]*=[ \t]*)\$\{\{\s*vars\.' + variable + r'\s*\}\}[ \t]*$'
                    config = re.sub(pattern, lambda match: match[1] + (KEY if variable == 'ATTIC_PUBLIC_KEY' else ''), config)
                    if 'vars.' + variable in config:
                        raise MigrationError('Mixed Attic Nix configuration requires manual repair')
                settings[field] = config
            rewritten.append(step)
        # Some native jobs only had an isolated publisher and never read either
        # private cache. Add the reader to this known package-build shape too;
        # PR events still build with public caches alone.
        native_build = next((i for i, step in enumerate(rewritten)
                             if 'target=".#packages.$TARGET_SYSTEM.$TARGET_PACKAGE"' in step.get('run', '')
                             and 'nix build "$target"' in step.get('run', '')
                             and {'TARGET_SYSTEM', 'TARGET_PACKAGE'} <= set(step.get('env', {}))), None)
        if native_build is not None and not has_reader:
            rewritten.insert(native_build, {
                'name': 'Configure read-only Niks3 access with GitHub OIDC',
                'if': "${{ (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}",
                'run': 'python3 ci/configure-niks3.py --oidc',
            })
            has_reader = True
        if has_publisher and not any(step.get('run', '').strip() == 'bash ci/install-niks3.sh' for step in rewritten):
            index = next((i for i, step in enumerate(rewritten) if 'ci/transfer-attic.py import' in step.get('run', '') or command(step.get('run', ''), 'push-niks3.py') is not None), len(rewritten))
            rewritten.insert(index, {'name': 'Install pinned Niks3 publisher before importing build data', 'run': 'bash ci/install-niks3.sh'})
        if has_publisher:
            install = next(i for i, step in enumerate(rewritten) if step.get('run', '').strip() == 'bash ci/install-niks3.sh')
            if not has_reader:
                # The isolated publisher also needs substitution for its trusted
                # bootstrap client. Keep the publisher's step guard as well as
                # the job guard; an unguarded job may also run on PR events.
                gates = {step.get('if', '') for step in rewritten
                         if command(step.get('run', ''), 'push-niks3.py') is not None}
                if len(gates) != 1:
                    raise MigrationError('Publisher reader requires one reviewed guard: ' + str(job_name))
                reader = {'name': 'Read Niks3 before installing the pinned publisher',
                          'run': 'python3 ci/configure-niks3.py --oidc'}
                gate = gates.pop()
                if gate:
                    reader['if'] = gate
                rewritten.insert(install, reader)
                has_reader = True
            else:
                auth = [command(step.get('run', ''), 'configure-niks3.py') for step in rewritten[:install]
                        if command(step.get('run', ''), 'configure-niks3.py') is not None]
                if not auth or auth[-1] != ['--oidc']:
                    raise MigrationError('Active publisher reader must precede client installation: ' + str(job_name))
        if has_reader:
            if not any(command(step.get('run', ''), 'configure-niks3.py') == ['--cleanup'] for step in rewritten):
                index = next((i for i, step in enumerate(rewritten) if 'ci/transfer-attic.py export' in step.get('run', '')), len(rewritten))
                rewritten.insert(index, {'name': 'Remove job-local Niks3 reader before exporting build data' if native_build is not None else 'Remove job-local Niks3 reader', 'if': '${{ always() }}', 'run': 'python3 ci/configure-niks3.py --cleanup'})
        if has_reader or has_publisher:
            permissions(document, job)
        if 'steps' in job:
            job['steps'] = rewritten
        remaining = json.dumps(job)
        if any('steps.' + name + '.' in remaining for name in removed_ids):
            raise MigrationError('Removed cache step still has referenced outputs: ' + str(job_name))
    retain_artifact_builds(document)
    document = cache_flags(document)
    def check_environments(value):
        if isinstance(value, dict):
            env = value.get('env', {})
            if isinstance(env, dict) and CREDENTIAL_KEYS.intersection(env):
                raise MigrationError('Unrecognized inherited static cache environment requires manual repair')
            for item in value.values():
                check_environments(item)
        elif isinstance(value, list):
            for item in value:
                check_environments(item)
    check_environments(document)
    remaining = json.dumps(document)
    if re.search(r'secrets\.(?:ATTIC_TOKEN|FLAKEY_ATTIC_TOKEN|ATTIC_PULL_TOKEN)\b', remaining):
        raise MigrationError('Unrecognized static cache secret reference requires manual repair')
    return document, manual


def retain_artifact_builds(document):
    """Keep partial cache data, while build results continue to block promotion."""
    builds = set()
    for name, job in document['jobs'].items():
        steps = job.get('steps', [])
        native = [step for step in steps
                  if ('target=".#packages.$TARGET_SYSTEM.$TARGET_PACKAGE"' in step.get('run', '')
                      or ('target=$(python3 ci/native-target.py package)' in step.get('run', '')
                          and 'check=$(python3 ci/native-target.py check)' in step.get('run', '')))
                  and 'nix build "$target"' in step.get('run', '')
                  and {'TARGET_SYSTEM', 'TARGET_PACKAGE'} <= set(step.get('env', {}))]
        exports = [step for step in steps if command(step.get('run', ''), 'transfer-attic.py') is not None
                   and command(step.get('run', ''), 'transfer-attic.py')[:1] == ['export']]
        if not native or not exports:
            continue
        if len(native) != 1 or len(exports) != 1:
            raise MigrationError('Review multiple native build/export steps: ' + str(name))
        export = exports[0]
        args = command(export['run'], 'transfer-attic.py')
        if (args[:2] != ['export', '--directory'] or len(args) not in {3, 5}
                or not re.fullmatch(r'[A-Za-z0-9_/-]+', args[2])
                or (len(args) == 5 and args[3:] != ['--manifest', 'cache-outputs.txt'])):
            raise MigrationError('Review nonstandard native export: ' + str(name))
        directory = args[2].rstrip('/')
        uploads = [step for step in steps if step.get('uses', '').startswith('actions/upload-artifact@')
                   and step.get('with', {}).get('path', '').rstrip('/') == directory]
        if len(uploads) != 1 or any(step is not export and step.get('id') == 'cache_transfer' for step in steps):
            raise MigrationError('Review native artifact upload: ' + str(name))
        if 'id' in export and export['id'] != 'cache_transfer':
            raise MigrationError('Review referenced export step: ' + str(name))
        export_gate = "${{ always() && !cancelled() && hashFiles('cache-outputs.txt') != '' }}"
        upload_gate = "${{ always() && !cancelled() && steps.cache_transfer.outcome == 'success' }}"
        if export.get('if', '') not in {'', 'success()', '${{ success() }}', export_gate} or uploads[0].get('if', '') not in {'', 'success()', '${{ success() }}', upload_gate}:
            raise MigrationError('Review additional native artifact guards: ' + str(name))
        if 'ci/retain-build-outputs.py' not in native[0]['run']:
            if 'target=$(python3 ci/native-target.py package)' in native[0]['run']:
                original = native[0]['run']
                if 'FLAKEY_CACHE_BUILD' in original:
                    raise MigrationError('Review native script delimiter: ' + str(name))
                native[0]['run'] = ('set -euo pipefail\n'
                    'target=$(python3 ci/native-target.py package)\n'
                    'check=$(python3 ci/native-target.py check)\n'
                    'cat > "$RUNNER_TEMP/native-cache-build.sh" <<\'FLAKEY_CACHE_BUILD\'\n'
                    + original.rstrip() + '\nFLAKEY_CACHE_BUILD\n'
                    'python3 ci/retain-build-outputs.py --target "$target" --target "$check" '
                    '--manifest cache-outputs.txt --script "$RUNNER_TEMP/native-cache-build.sh"\n')
            else:
                native[0]['run'] = native[0]['run'].replace('nix build "$target"',
                    'python3 ci/retain-build-outputs.py --target "$target" --manifest cache-outputs.txt -- nix build "$target"', 1)
        export.update(id='cache_transfer',
            run='python3 ci/transfer-attic.py ' + shlex.join(args[:3] + ['--manifest', 'cache-outputs.txt']),
            **{'if': export_gate})
        uploads[0]['if'] = upload_gate
        builds.add(name)
    for name, job in document['jobs'].items():
        needs = job.get('needs', [])
        needs = [needs] if isinstance(needs, str) else needs
        dependencies = builds.intersection(needs)
        if not dependencies or not any(command(step.get('run', ''), 'push-niks3.py') is not None
                                       for step in job.get('steps', [])):
            continue
        gate = job.get('if', '')
        if gate.startswith('${{') and gate.endswith('}}'):
            gate = gate[3:-2].strip()
        if 'success()' in gate:
            raise MigrationError('Review explicit successful-build predicate: ' + str(name))
        for build in dependencies:
            pattern = r'needs\.' + re.escape(build) + r"\.result\s*==\s*(['\"])success\1"
            condition = f"(needs.{build}.result == 'success' || needs.{build}.result == 'failure')"
            if condition not in gate:
                if re.search(pattern, gate):
                    gate = re.sub(pattern, lambda _: condition, gate)
                else:
                    gate = condition + ' && (' + gate + ')'
        if 'always()' not in gate:
            gate = 'always() && !cancelled() && (' + gate + ')'
        job['if'] = '${{ ' + gate + ' }}'


def migrate_directory(directory, controller_revision=None, check=False):
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise MigrationError('--directory must name an existing owning repository')
    if controller_revision is not None and not re.fullmatch(r'[a-f0-9]{40}', controller_revision):
        raise MigrationError('--controller-revision must be an exact lowercase 40-character commit')
    report = {'changed': [], 'manual_review': [], 'skipped': [], 'errors': []}
    planned = []
    if (directory / '.github').is_symlink() or (directory / '.github/workflows').is_symlink():
        raise MigrationError('Workflow directory symlinks require manual review')
    for path in sorted((directory / '.github/workflows').glob('*')):
        if path.suffix not in {'.yml', '.yaml'}:
            continue
        relative = str(path.relative_to(directory))
        if path.name in {'release-channels.yml', 'release-channels.yaml'}:
            report['skipped'].append(relative)
            continue
        try:
            if path.is_symlink():
                raise MigrationError('Workflow symlinks require manual review')
            before = loads(path.read_text())
            after, manual = migrate_document(before, controller_revision)
            report['manual_review'].extend(relative + ': ' + item for item in manual)
            if after != before:
                planned.append((path, dumps(after)))
                report['changed'].append(relative)
        except (ValueError, TypeError, OSError) as error:
            report['errors'].append(relative + ': ' + str(error))
    if report['errors']:
        return report
    if not check:
        for path, text in planned:
            path.write_text(text)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--controller-revision')
    parser.add_argument('--check', action='store_true', help='Report required edits without writing; exit 1 when changes are needed')
    args = parser.parse_args(argv)
    try:
        report = migrate_directory(args.directory, args.controller_revision, args.check)
    except (ValueError, ImportError) as error:
        print(json.dumps({'errors': [str(error)]}))
        return 2
    print(json.dumps(report, indent=2))
    return 2 if report['errors'] else 1 if args.check and report['changed'] else 0


if __name__ == '__main__':
    sys.exit(main())
