#!/usr/bin/env python3
"""Trusted evaluator for a release-channel inventory. Never imports candidate code."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

CODE = Path(__file__).resolve().parent

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def evaluation_environment(controller=False):
    # Explicit allowlist: request authenticators and forge tokens never reach
    # Nix, Git, fetchers or their subprocesses. Worker reader configuration is
    # already built from scratch; only that fixed config survives here.
    names = {'PATH','HOME','TMPDIR','LANG','LC_ALL','NIX_REMOTE','NIX_CONFIG',
             'SSL_CERT_FILE','NIX_SSL_CERT_FILE','GIT_SSL_CAINFO','GIT_CONFIG_GLOBAL',
             'GIT_CONFIG_NOSYSTEM','GIT_TERMINAL_PROMPT','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_STATE_HOME'}
    env = {k:v for k,v in os.environ.items() if k in names}
    if controller:
        env.update(NIX_CONFIG='experimental-features = nix-command flakes\naccess-tokens =\nnetrc-file = /dev/null\nbuilders =\nmax-jobs = 0\n',
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    return env

def evaluate(repo, full=True, system='aarch64-darwin'):
    if system not in ('aarch64-darwin','x86_64-darwin'):
        raise ValueError('Unsupported Darwin system')
    env = evaluation_environment(controller=not full)
    def command(*args):
        return subprocess.check_output(args, cwd=repo, env=env, text=True, timeout=2700).strip()
    def nix(expression):
        return json.loads(command('nix', 'eval', '--json', *(['--offline','--option','restrict-eval','true'] if full else []), '--option', 'accept-flake-config', 'false',
            '--option', 'allow-import-from-derivation', 'false', '--builders', '', '--max-jobs', '0', '--expr', expression))
    commit = command('git', 'rev-parse', 'HEAD')
    identity = dict(commit=commit, tree=command('git','rev-parse','HEAD^{tree}'),
                    lockHash=hashlib.sha256((repo/'flake.lock').read_bytes()).hexdigest())
    policy = json.loads((repo/'ci/channels.json').read_text())
    if policy['forge'] != 'forgejo' or policy.get('submodules'):
        raise ValueError('Only Forgejo tools without submodules are supported')
    uri = 'git+file://'+str(repo.resolve())+'?rev='+commit
    prefix = 'let f = builtins.getFlake '+json.dumps(uri)+'; in '
    attrs = [t['attribute'] for t in policy['targets'][system]]
    attrs += ['checks.'+system+'.'+n for n in nix(prefix+'builtins.attrNames (f.checks.'+system+' or {})')]
    roots, aliases = {}, {}
    for attr in attrs:
        # Attribute spelling is data, never interpolated as Nix syntax.
        select = 'builtins.foldl\' (v: n: builtins.getAttr n v) f '+'(builtins.fromJSON '+json.dumps(json.dumps(attr.split('.')))+')'
        outputs = nix(prefix+'let d = '+select+'; in builtins.listToAttrs (map (o: { name = o; value = { drv = d.drvPath; output = o; path = d.${o}.outPath; }; }) (d.outputs or [ "out" ]))')
        for output, item in outputs.items():
            name = 'root-'+digest(item['drv']+'^'+output)[:24]
            roots[name] = item
            aliases.setdefault(name, []).append({'attribute':attr})
    graph = json.loads(command('nix', 'derivation', 'show', '--recursive', *sorted({r['drv'] for r in roots.values()})))
    manifest = {**identity, 'system':system, 'repository':policy['repository'], 'policyHash':digest(policy),
                'roots':roots, 'aliases':aliases, 'graphHash':digest(graph)}
    if not full:
        return manifest
    source = nix(prefix+'f.outPath')
    # Construct the publication anchor entirely in installed code. Using the
    # candidate's nixpkgs.runCommand would let it change the anchor builder.
    publication = """let
      roots = builtins.fromJSON ROOTS;
      paths = map (r: builtins.appendContext r.path { ${r.drv} = { outputs = [ r.output ]; }; }) roots;
      d = builtins.derivation {
        name = "tool-darwin-publication"; system = SYSTEM;
        builder = "/bin/sh"; PATH = "/bin:/usr/bin"; rootPaths = paths;
        args = [ "-c" ''
          mkdir -p "$out"
          n=0
          for path in $rootPaths; do
            ln -s "$path" "$out/root-$n"
            n=$((n+1))
          done
        '' ];
      };
    in { drv = d.drvPath; path = d.outPath; output = "out"; }
    """.replace('ROOTS', json.dumps(json.dumps(list(roots.values())))).replace('SYSTEM',json.dumps(system))
    executor = json.loads((CODE/'ci/fleet-targets.json').read_text())['executors']['aarch64-darwin']
    return {'manifest':manifest, 'sourcePath':source,
            'publicationRoot':nix(publication), 'executor':executor}

if __name__ == '__main__':
    print(json.dumps(evaluate(Path(sys.argv[1]), '--manifest' not in sys.argv, sys.argv[sys.argv.index('--system')+1] if '--system' in sys.argv else 'aarch64-darwin')))
