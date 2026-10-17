#!/usr/bin/env python3
"""Execute the reviewed policy in a disposable, credential-free build job."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fork_ci import Blocked, validate_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--system', required=True)
    parser.add_argument('--outputs', type=Path, required=True)
    args = parser.parse_args()
    policy = validate_policy(json.loads((args.source / '.fork-ci.json').read_text()))
    row = next(r for r in policy['matrix'] if r['system'] == args.system)
    env = {k: v for k, v in os.environ.items() if k not in
           ['GH_TOKEN', 'GITHUB_TOKEN', 'ATTIC_TOKEN', 'SSH_AUTH_SOCK']}
    if os.environ.get('ATTIC_TOKEN') or os.environ.get('GH_TOKEN'):
        raise Blocked('Build jobs must not receive publication credentials')
    flags = ['--option', 'accept-flake-config', 'false', '--option', 'builders', '',
             '--option', 'substituters', 'https://cache.nixos.org',
             '--option', 'extra-substituters', '']
    packages = ['.#packages.' + args.system + '.' + name for name in row['packages']]
    result = subprocess.check_output(['nix', 'build', '--no-link', '--json', *flags, *packages],
                                     cwd=args.source, env=env)
    roots = sorted({path for output in json.loads(result) for path in output['outputs'].values()})
    checks = ['.#checks.' + args.system + '.' + name for name in row.get('checks', [])]
    if checks:
        subprocess.run(['nix', 'build', '--no-link', *flags, *checks],
                       cwd=args.source, env=env, check=True)
    for command in row.get('tests', []):
        subprocess.run(command, cwd=args.source, env=env, check=True)
    args.outputs.write_text('\n'.join(roots) + '\n')
    print('All selected package builds, checks and test commands passed.')


if __name__ == '__main__':
    raise SystemExit(main())
