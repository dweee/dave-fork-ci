#!/usr/bin/env python3
"""Execute the reviewed policy in a disposable, credential-free build job."""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fork_ci import Blocked, validate_policy


def public_cache(env):
    """Use only the operator's public read endpoint and verification key."""
    endpoint = env.get('ATTIC_SUBSTITUTER', '')
    key = env.get('ATTIC_PUBLIC_KEY', '')
    if not endpoint and not key:
        return ''
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError:
        raise Blocked('Public substitution requires a valid HTTPS URL') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in endpoint)):
        raise Blocked('Public substitution requires an HTTPS URL without credentials')
    try:
        name, encoded = key.split(':', 1)
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', name):
            raise ValueError()
        if len(base64.b64decode(encoded, validate=True)) != 32:
            raise ValueError()
    except (ValueError, binascii.Error):
        raise Blocked('Public substitution requires a valid Nix verification key') from None
    # Test commands such as nix develop inherit the same public settings. Values
    # are validated before putting them into the line-oriented Nix config.
    env['NIX_CONFIG'] = (env.get('NIX_CONFIG', '') + '\naccept-flake-config = false\n'
                         + 'extra-substituters = ' + endpoint + '\n'
                         + 'extra-trusted-public-keys = ' + key + '\n')
    return endpoint


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
    substituter = public_cache(env)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.source, env=env).strip()
    flags = ['--no-update-lock-file', '--no-write-lock-file',
             '--option', 'accept-flake-config', 'false', '--option', 'builders', '',
             '--option', 'substituters', 'https://cache.nixos.org',
             '--option', 'extra-substituters', substituter]
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
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.source, env=env).strip() != head:
        raise Blocked('Tests changed the candidate revision')
    subprocess.run(['git', 'diff', '--no-ext-diff', '--no-textconv', '--exit-code', 'HEAD', '--ignore-submodules=none', '--'],
                   cwd=args.source, env=env, check=True)
    args.outputs.write_text('\n'.join(roots) + '\n')
    print('All selected package builds, checks and test commands passed.')


if __name__ == '__main__':
    raise SystemExit(main())
