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
    def test_completed_checks_are_cached_before_later_release_failure(self):
        root = '/nix/store/'+'a'*32+'-package'
        check = '/nix/store/'+'b'*32+'-check'
        uploads = []
        def run(*args, **kwargs):
            if args[:2] == ('nix', 'eval'):
                return 'x86_64-linux' if '--impure' in args else json.dumps(['x86_64-linux'])
            if args[:3] == ('nix', 'derivation', 'show'):
                path = check if '#checks.' in args[4] else root
                return json.dumps({'/nix/store/'+'c'*32+'-fixture.drv': {'outputs': {'out': {'path': path}}}})
            if args[:4] == ('nix', 'path-info', '--json', '--all'):
                return json.dumps({root: {}, check: {}})
            if args[:2] == ('nix', 'build'):
                return check if '#checks.' in args[2] else root
            if 'push-niks3.py' in args[1]:
                uploads.append(Path(args[-1]).read_text().splitlines())
                return ''
            raise AssertionError(args)
        rules = {'forge': 'forgejo', 'targets': {'x86_64-linux': [
            {'attribute': 'packages.x86_64-linux.default', 'visibility': 'private'}]}}
        with patch.object(channel, 'policy', return_value=rules), patch.object(channel, 'source', return_value='a'*40), patch.object(channel, 'run', side_effect=run), patch.object(channel, 'info', return_value={root: {}}), patch.object(channel, 'native_check_names', return_value=['smoke']), patch.object(channel, 'verify', side_effect=ValueError('later verification failed')):
            with self.assertRaisesRegex(ValueError, 'later verification failed'):
                channel.build('x86_64-linux')
        self.assertEqual(uploads, [[root], [check]])

    def test_release_does_not_install_an_attic_client(self):
        calls=[]
        def run(*args,**kwargs):
            calls.append(args)
            if args[:2]==('nix','eval'):
                return 'aarch64-darwin' if '--impure' in args else json.dumps(['aarch64-darwin'])
            return ''
        rules={'forge':'github','targets':{'aarch64-darwin':[]}}
        with patch.object(channel,'policy',return_value=rules),patch.object(channel,'source',return_value='a'*40),patch.object(channel,'run',side_effect=run),patch.object(channel,'native_check_names',return_value=[]),patch.object(channel,'verify',return_value='a'*64):
            with self.assertRaisesRegex(ValueError, 'Missing built output closure'):
                channel.build('aarch64-darwin')
        self.assertFalse(any('nixpkgs#attic-client' in args for args in calls))
    def test_niks3_installer_registers_a_job_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binary=root/'nix';client=root/'store-client';(client/'bin').mkdir(parents=True)
            (client/'bin/niks3').write_text('#!/bin/sh\nexit 0\n');(client/'bin/niks3').chmod(0o700)
            binary.write_text('#!/bin/sh\nset -eu\ntest -z "${NIKS3_AUTH_TOKEN_FILE+x}${NIKS3_WRITE_TOKEN+x}${GH_TOKEN+x}${ACTIONS_ID_TOKEN_REQUEST_TOKEN+x}"\ntest "$NIKS3_NETRC_FILE" = readonly-fixture\nroot=\nwhile [ "$#" -gt 0 ]; do case "$1" in --no-link) exit 42;; --out-link) shift; root=$1;; esac; shift; done\ntest -n "$root"\nln -s "$FIXTURE_CLIENT" "$root"\nprintf "%s\\n" "$FIXTURE_CLIENT"\n');binary.chmod(0o700)
            env=os.environ|{'PATH':str(root)+':'+os.environ['PATH'],'RUNNER_TEMP':str(root),'FIXTURE_CLIENT':str(client),'GITHUB_ENV':str(root/'env'),
                            'NIKS3_AUTH_TOKEN_FILE':'synthetic-writer-file','NIKS3_WRITE_TOKEN':'synthetic-writer',
                            'GH_TOKEN':'synthetic-forge','ACTIONS_ID_TOKEN_REQUEST_TOKEN':'synthetic-request','NIKS3_NETRC_FILE':'readonly-fixture'}
            subprocess.run(['bash',str(ROOT/'ci/install-niks3.sh')],env=env,check=True)
            self.assertEqual((root/'niks3-client').resolve(),client)
            self.assertIn('NIKS3_BIN='+str(client)+'/bin/niks3',(root/'env').read_text())

    def test_bootstrap_retention_is_read_only_and_requires_an_explicit_flag(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client=root/'client';(client/'bin').mkdir(parents=True)
            (client/'bin/niks3').write_text('fixture');(client/'bin/niks3').chmod(0o700)
            python=root/'python3'
            python.write_text('#!'+sys.executable+'\n'+'''import os, pathlib, sys
assert pathlib.Path(sys.argv[1]).name == 'retain-build-outputs.py'
for name in ('NIKS3_AUTH_TOKEN_FILE','NIKS3_WRITE_TOKEN','GH_TOKEN','GITHUB_TOKEN','CHANNEL_TOKEN','SOURCE_TOKEN','SSH_AUTH_SOCK','ACTIONS_ID_TOKEN_REQUEST_TOKEN','ACTIONS_ID_TOKEN_REQUEST_URL'):
    assert name not in os.environ, name
assert os.environ['NIKS3_NETRC_FILE']=='readonly-fixture'
target='github:Mic92/niks3/2d5a6b14e0df5c8fecc27571b79c40563c98ec32#niks3'
assert sys.argv[sys.argv.index('--target')+1]==target
assert sys.argv[sys.argv.index('--')+1:][:2]==['nix','build']
assert '--out-link' in sys.argv and target in sys.argv
pathlib.Path(sys.argv[sys.argv.index('--manifest')+1]).write_text(os.environ['FIXTURE_CLIENT']+'\\n')
print(os.environ['FIXTURE_CLIENT'])
''');python.chmod(0o700)
            env=os.environ|{'PATH':str(root)+':'+os.environ['PATH'],'RUNNER_TEMP':str(root),'FIXTURE_CLIENT':str(client),'GITHUB_ENV':str(root/'env'),
                            'NIKS3_AUTH_TOKEN_FILE':'synthetic-writer-file','NIKS3_WRITE_TOKEN':'synthetic-writer','GH_TOKEN':'synthetic-forge',
                            'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'synthetic-request','NIKS3_NETRC_FILE':'readonly-fixture'}
            script=str(ROOT/'ci/install-niks3.sh')
            result=subprocess.run(['bash',script,'--unknown'],cwd=root,env=env,capture_output=True)
            self.assertEqual(result.returncode,2);self.assertFalse((root/'niks3-bootstrap-outputs.txt').exists())
            subprocess.run(['bash',script,'--retain-bootstrap'],cwd=root,env=env,check=True)
            self.assertEqual((root/'niks3-bootstrap-outputs.txt').read_text(),str(client)+'\n')
            self.assertIn('NIKS3_BIN='+str(client)+'/bin/niks3',(root/'env').read_text())
    def test_shared_publisher_uses_rooted_niks3_before_import(self):
        workflow=(ROOT/'.github/workflows/upstream-sync.yml').read_text()
        cache=workflow.split('\n  cache:',1)[1].split('\n  promote:',1)[0]
        self.assertLess(cache.index('install-niks3.sh'),cache.index('Import data'))
        self.assertLess(cache.index('configure-niks3.py --oidc'),cache.index('install-niks3.sh'))
        self.assertIn('install-niks3.sh --retain-bootstrap',cache)
        self.assertLess(cache.index('push-niks3.py --outputs niks3-bootstrap-outputs.txt'),cache.index('Import data'))
        self.assertLess(cache.index('push-niks3.py'),cache.index('configure-niks3.py --cleanup'))
        self.assertNotIn('nixpkgs#attic-client',cache)
        self.assertNotIn('push-attic.py',cache)
    def test_installer_roots_native_publishers_without_reformatting(self):
        spec=importlib.util.spec_from_file_location('install_roots',ROOT/'ci/install-channel-automation.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'.github/workflows';folder.mkdir(parents=True)
            workflow=folder/'native.yml';workflow.write_text('name: Native\n# Preserve native layout\njobs:\n  publish:\n    steps:\n      - run: |\n          client=$(nix build nixpkgs#attic-client --no-link --print-out-paths)\n')
            policy=root/'policy.json';policy.write_text('{"targets":{}}')
            yaml=SimpleNamespace(SafeDumper=SimpleNamespace(add_representer=lambda *a:None), safe_dump=lambda value,**kw:json.dumps(value))
            with patch.dict('sys.modules',{'yaml':yaml}):
                mod.install(SimpleNamespace(directory=root,forge='github',repository='fixture/tool',branch='main',policy=policy))
            text=workflow.read_text();self.assertIn('# Preserve native layout',text)
            self.assertIn('--out-link "${RUNNER_TEMP:-/tmp}/attic-client"',text)
            self.assertNotIn('--no-link',text)

if __name__=='__main__':unittest.main()
