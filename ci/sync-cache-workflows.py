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
CACHE_CONF = runpy.run_path(str(HERE/'cache-config.py'))['nix_config']()


def config(value):
    if not isinstance(value, str) or '${{' in value:
        raise ValueError('Dynamic NIX_CONFIG requires manual review')
    value = re.sub(r'(?m)^(?:extra-)?(?:substituters|trusted-substituters|trusted-public-keys)\s*=.*\n?', '', value)
    return value.rstrip()+'\n'+CACHE_CONF


def apply(document):
    document = copy.deepcopy(document)
    manual = []
    for name, job in document.get('jobs', {}).items():
        steps = job.get('steps', [])
        nix_job = any('nix' in str(step.get('uses', '')).split('@', 1)[0].lower()
                      or re.search(r'\bnix(?:-shell|-store|-build|-instantiate|-env)?\s', str(step.get('run', '')))
                      for step in steps)
        if not nix_job:
            continue
        inherited = document.get('env', {}).get('NIX_CONFIG', '')
        job.setdefault('env', {})['NIX_CONFIG'] = config(job.get('env', {}).get('NIX_CONFIG', inherited))
        effective = document.get('env', {}) | job.get('env', {})
        if str(effective.get('CI_NIX_DAEMON_MANAGED')) == '1':
            job['env']['NIX_CONFIG'] += 'extra-substituters = https://niks3.monke.fail\n'
        for step in steps:
            env = step.get('env', {})
            if 'NIX_CONFIG' in env:
                env['NIX_CONFIG'] = config(env['NIX_CONFIG'])
                if str(effective.get('CI_NIX_DAEMON_MANAGED')) == '1':
                    env['NIX_CONFIG'] += 'extra-substituters = https://niks3.monke.fail\n'
            action = str(step.get('uses', '')).split('@', 1)[0]
            field = ('nix_conf' if action == 'nixbuild/nix-quick-install-action' else
                     'extra-conf' if action in {'DeterminateSystems/determinate-nix-action',
                                               'DeterminateSystems/nix-installer-action'} else None)
            if field:
                settings = step.setdefault('with', {})
                settings[field] = config(settings.get(field, ''))
            run = str(step.get('run', ''))
            if re.search(r'--option\s+(?:extra-)?substituters|(?:^|\n)\s*(?:export\s+)?NIX_CONFIG=', run):
                manual.append(str(name)+': explicit command cache override; retain proof-only restrictions, review build overrides')
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
