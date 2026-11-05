"""Publishers must survive runner GC between install and final upload."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_roots',ROOT/'ci/release-channel.py')
channel=importlib.util.module_from_spec(spec);spec.loader.exec_module(channel)

class PublisherRoots(unittest.TestCase):
    def test_release_attic_client_is_rooted_for_the_release_lifetime(self):
        class Installed(Exception):pass
        calls=[]
        def run(*args,**kwargs):
            if args[:2]==('nix','eval'):
                return 'aarch64-darwin' if '--impure' in args else json.dumps(['aarch64-darwin'])
            calls.append(args)
            self.assertIn('nixpkgs#attic-client',args)
            self.assertNotIn('--no-link',args)
            root=Path(args[args.index('--out-link')+1])
            self.assertEqual(root.name,'attic-client')
            self.assertTrue(root.parent.is_dir())
            raise Installed
        rules={'forge':'github','targets':{'aarch64-darwin':[]}}
        with patch.object(channel,'policy',return_value=rules),patch.object(channel,'source',return_value='a'*40),patch.object(channel,'run',side_effect=run):
            with self.assertRaises(Installed):channel.build('aarch64-darwin')
        self.assertEqual(len(calls),1)
    def test_niks3_installer_registers_a_job_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binary=root/'nix';client=root/'store-client';(client/'bin').mkdir(parents=True)
            (client/'bin/niks3').write_text('#!/bin/sh\nexit 0\n');(client/'bin/niks3').chmod(0o700)
            binary.write_text('#!/bin/sh\nset -eu\nroot=\nwhile [ "$#" -gt 0 ]; do case "$1" in --no-link) exit 42;; --out-link) shift; root=$1;; esac; shift; done\ntest -n "$root"\nln -s "$FIXTURE_CLIENT" "$root"\nprintf "%s\\n" "$FIXTURE_CLIENT"\n');binary.chmod(0o700)
            env=os.environ|{'PATH':str(root)+':'+os.environ['PATH'],'RUNNER_TEMP':str(root),'FIXTURE_CLIENT':str(client),'GITHUB_ENV':str(root/'env')}
            subprocess.run(['bash',str(ROOT/'ci/install-niks3.sh')],env=env,check=True)
            self.assertEqual((root/'niks3-client').resolve(),client)
            self.assertIn('NIKS3_BIN='+str(client)+'/bin/niks3',(root/'env').read_text())
    def test_shared_attic_publisher_has_a_job_root(self):
        workflow=(ROOT/'.github/workflows/upstream-sync.yml').read_text()
        install=workflow.split('client=$(nix build',1)[1].split(')',1)[0]
        self.assertIn('--out-link "$RUNNER_TEMP/attic-client"',install)
        self.assertNotIn('--no-link',install)
    def test_installer_roots_native_publishers_without_reformatting(self):
        spec=importlib.util.spec_from_file_location('install_roots',ROOT/'ci/install-channel-automation.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'.github/workflows';folder.mkdir(parents=True)
            workflow=folder/'native.yml';workflow.write_text('name: Native\n# Preserve native layout\njobs:\n  publish:\n    steps:\n      - run: |\n          client=$(nix build nixpkgs#attic-client --no-link --print-out-paths)\n')
            policy=root/'policy.json';policy.write_text('{"targets":{}}')
            mod.install(SimpleNamespace(directory=root,forge='github',repository='fixture/tool',branch='main',policy=policy))
            text=workflow.read_text();self.assertIn('# Preserve native layout',text)
            self.assertIn('--out-link "${RUNNER_TEMP:-/tmp}/attic-client"',text)
            self.assertNotIn('--no-link',text)

if __name__=='__main__':unittest.main()
