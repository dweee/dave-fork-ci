#!/usr/bin/env python3
"""Apply only the public cache policy to existing GitHub or Forgejo jobs.

This does not add readers, publishers, credentials, permissions or triggers.
Use the channel installer for generated channel workflows instead.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import runpy

HERE = Path(__file__).resolve().parent
CACHE = runpy.run_path(str(HERE/'cache-config.py'))
config = CACHE['transform']


def apply(document):
    document = copy.deepcopy(document)
    manual = []
    for name, job in document.get('jobs', {}).items():
        steps = job.get('steps', [])
        if not CACHE['nix_job'](steps):
            continue
        if any('configure-attic-read.py' in str(step.get('run', '')) for step in steps):
            manual.append(str(name)+': legacy Attic reader: migrate to canonical Niks3 reader in owning repository before sync')
            continue
        inherited = document.get('env', {}).get('NIX_CONFIG', '')
        original = copy.deepcopy(job)
        try:
            job.setdefault('env', {})['NIX_CONFIG'] = config(job.get('env', {}).get('NIX_CONFIG', inherited))
        except ValueError as error:
            manual.append(str(name)+': env.NIX_CONFIG: '+str(error))
            document['jobs'][name] = original
            continue
        effective = document.get('env', {}) | job.get('env', {})
        if str(effective.get('CI_NIX_DAEMON_MANAGED')) == '1':
            job['env']['NIX_CONFIG'] += 'extra-substituters = https://niks3.monke.fail\n'
        for step in steps:
            env = step.get('env', {})
            if 'NIX_CONFIG' in env:
                try:
                    env['NIX_CONFIG'] = config(env['NIX_CONFIG'])
                except ValueError as error:
                    manual.append(str(name)+': step.env.NIX_CONFIG: '+str(error))
                    document['jobs'][name] = original
                    break
                if str(effective.get('CI_NIX_DAEMON_MANAGED')) == '1':
                    env['NIX_CONFIG'] += 'extra-substituters = https://niks3.monke.fail\n'
            field = CACHE['installer_field'](step)
            if field:
                settings = step.setdefault('with', {})
                try:
                    settings[field] = config(settings.get(field, ''))
                except ValueError as error:
                    manual.append(str(name)+': '+field+': '+str(error))
                    document['jobs'][name] = original
                    break
            run = str(step.get('run', ''))
            if re.search(r'--option\s+(?:extra-)?substituters|(?:^|\n)\s*(?:export\s+)?NIX_CONFIG=', run):
                manual.append(str(name)+': explicit command cache override; retain proof-only restrictions, review build overrides')
        if document['jobs'][name] is not original and any(CACHE['installer_field'](step) for step in steps):
            old = original.get('env', {}).get('NIX_CONFIG', inherited)
            kept = CACHE['without_cache'](old)
            if kept.strip() or inherited.strip():
                job['env']['NIX_CONFIG'] = kept+'\n'
            else:
                job['env'].pop('NIX_CONFIG', None)
            if not job['env']:
                job.pop('env')
    return document, sorted(set(manual))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--forge', choices=['github','forgejo'], required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    migration = runpy.run_path(str(HERE/'migrate-niks3-workflows.py'))
    folder = args.directory/('.github/workflows' if args.forge == 'github' else '.forgejo/workflows')
    if args.forge == 'forgejo' and not folder.is_dir():
        folder = args.directory/'.github/workflows'  # Forgejo fallback when .forgejo is absent.
    planned=[];manual=[]
    for path in sorted(folder.glob('*.y*ml')):
        if path.is_symlink():
            raise ValueError('Workflow symlinks require manual review')
        before = migration['loads'](path.read_text())
        after, notes = apply(before)
        manual.extend(str(path)+': '+note for note in notes)
        if before != after:
            planned.append((path, migration['dumps'](after)))
    if not args.check:
        for path, text in planned:
            path.write_text(text)
    print(json.dumps({'changed':[str(p) for p,_ in planned], 'manual_review':manual}, indent=2))
    return 1 if args.check and planned else 0


if __name__ == '__main__':
    raise SystemExit(main())
