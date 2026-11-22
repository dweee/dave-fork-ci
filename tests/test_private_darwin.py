import base64
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_channel_workflow_refs as workflow_fixture

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tool_darwin_test',ROOT/'ci/tool-darwin.py')
remote = importlib.util.module_from_spec(spec);spec.loader.exec_module(remote)

class PrivateDarwin(unittest.TestCase):
    def test_generator_routes_both_systems_and_wires_promotion_evidence(self):
        import tempfile
        route = {'attestorPublicKeys':['ssh-ed25519 TEST'], 'clientCommit':'a'*40,'providerCommit':'b'*40}
        with tempfile.TemporaryDirectory() as directory:
            workflow = workflow_fixture.ChannelWorkflowRefs().generated('forgejo',directory,
                systems=('aarch64-darwin','x86_64-darwin'),extra={'privateDarwin':route,'forgejoSharedDaemon':True,'forgejoDeterminateImage':workflow_fixture.installer.SHARED_DAEMON_IMAGE,'forgejoDeterminateLabels':['darwin-tools']})['release-channels']
        for system in ('aarch64_darwin','x86_64_darwin'):
            job = workflow['jobs']['build_'+system]
            self.assertEqual(job['runs-on'],'darwin-tools')
            self.assertIn('flakey-darwin-tool-tool-metadata:/run/darwin-tool-metadata',job['container']['volumes'])
            self.assertIn('flakey-ci-nix-pr-v2-ro:/nix:ro',job['container']['volumes'])
            self.assertIn('flakey-ci-nix-pr-socket-untrusted-ro:/nix/var/nix/daemon-socket:ro',job['container']['volumes'])
            self.assertFalse([v for v in job['container']['volumes'] if v.split(':',1)[0] in
                              ('flakey-ci-nix-pr-v2','flakey-ci-nix-pr-socket','flakey-ci-nix-pr-socket-untrusted','flakey-ci-nix-pr-creds')])
            self.assertNotIn('nix-daemon',str(job['steps']))
            self.assertIn('darwin',job['outputs'])
            release = next(s for s in job['steps'] if s.get('id')=='release')
            self.assertIn('PRIVATE_DARWIN_HMAC_KEY',release['env'])
            self.assertNotIn('NATIVE_BUILD_ADMISSION_COMMAND',str(job))
        promote=next(s for s in workflow['jobs']['promote']['steps'] if 'PRIVATE_DARWIN_PROOF_DIRECTORY' in s.get('env',{}))
        self.assertNotIn('PRIVATE_DARWIN_PROOFS',promote['env'])
        materialize=next(s for s in workflow['jobs']['promote']['steps'] if s.get('name')=='Materialize signed Darwin proof files')
        self.assertIn('x86_64-darwin.json',materialize['run'])

    def test_closure_rejects_mismatch_and_cache_adoption(self):
        roots = {'r':{'path':'root'}}
        metadata = {'root':{'narHash':'hash','narSize':1,'references':[]}}
        result = {'manifest':{'roots':roots},'closureHash':remote.p.digest(metadata)}
        release = SimpleNamespace(remote_closure=lambda *a:metadata,metadata=lambda e:e,digest=remote.p.digest,
            verify_published=lambda *a:remote.p.digest({'roots':['root'],'metadata':metadata}))
        self.assertEqual(remote.verify_closure(result,release,{}),metadata)
        result['closureHash']='0'*64
        with self.assertRaises(ValueError):remote.verify_closure(result,release,{})
        result['closureHash']=remote.p.digest(metadata)
        release.verify_published=lambda *a:'adopted-different-realisation'
        with self.assertRaises(ValueError):remote.verify_closure(result,release,{})

    def test_verify_binds_repository_commit_policy_system_executor_and_roots(self):
        policy={'repository':'d/tool','privateDarwin':{'attestorPublicKeys':['key'],'clientCommit':'a'*40,'providerCommit':'b'*40}}
        manifest={'repository':'d/tool','commit':'c'*40,'system':'aarch64-darwin','policyHash':remote.p.digest(policy),'roots':{'r':{}}}
        result={'action':'result','status':'built','manifest':manifest,'manifestHash':remote.p.digest(manifest),
            'targets':{'r':'built'},'executor':remote.p.HOSTED_EXECUTOR,'sessions':['b123456'],
            'client':'a'*40,'provider':'b'*40,'closureHash':'d'*64}
        encoded=base64.b64encode(b'{}').decode()
        with patch.object(remote.p,'verify_attestation',return_value=result):
            remote.verify(encoded,policy,'c'*40,'aarch64-darwin')
            for key,value in [('client','0'*40),('targets',{}),('closureHash','bad')]:
                old=result[key];result[key]=value
                with self.assertRaises(ValueError):remote.verify(encoded,policy,'c'*40,'aarch64-darwin')
                result[key]=old
            with self.assertRaises(ValueError):remote.verify(encoded,policy,'e'*40,'aarch64-darwin')
            with self.assertRaises(ValueError):remote.verify(encoded,policy,'c'*40,'x86_64-darwin')

if __name__ == '__main__':unittest.main()

class ReleaseBindings(unittest.TestCase):
    def setUp(self):
        import sys
        spec=importlib.util.spec_from_file_location('remote_release_binding',ROOT/'ci/release-channel.py')
        self.channel=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.channel;spec.loader.exec_module(self.channel)
        self.rules={'repository':'d/tool','remote':'https://git.monke.fail/d/tool.git','branch':'main',
            'targets':{'aarch64-darwin':[]},'privateDarwin':{'attestorPublicKeys':['ssh-ed25519 TEST']}}
        self.sha='a'*40
        self.result={'manifest':{'roots':{'r':{'path':'signed-root'}}}}

    def test_finish_remote_build_checks_roots_and_emits_receipt_only_on_success(self):
        import os,tempfile
        from contextlib import ExitStack
        c=self.channel
        for roots,success in [(['signed-root'],True),(['unsigned-root'],False)]:
            with self.subTest(roots=roots),tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
                output=Path(temporary)/'output'
                stack.enter_context(patch.dict(os.environ,{'GITHUB_OUTPUT':str(output),'CHANNEL_TOKEN':'sentinel'}))
                stack.enter_context(patch.object(c,'module',return_value=remote))
                stack.enter_context(patch.object(remote,'verify',return_value=self.result))
                verified=stack.enter_context(patch.object(remote,'verify_closure'))
                stack.enter_context(patch.object(c,'evaluated_roots',return_value=roots))
                stack.enter_context(patch.object(c,'remote_closure',return_value={}))
                stack.enter_context(patch.object(c,'verify_published',return_value='b'*64))
                stack.enter_context(patch.object(c,'source',return_value=self.sha))
                stack.enter_context(patch.object(c,'run'))
                pinned=stack.enter_context(patch.object(c,'pin_release',return_value='manifest'))
                if success:
                    c.finish_remote_build(self.rules,'aarch64-darwin',self.sha,'signed',dict(os.environ))
                    self.assertIn('darwin=signed',output.read_text());pinned.assert_called_once()
                    self.assertNotIn('CHANNEL_TOKEN',verified.call_args.args[2])
                else:
                    with self.assertRaisesRegex(ValueError,'absent from the signed manifest'):
                        c.finish_remote_build(self.rules,'aarch64-darwin',self.sha,'signed',dict(os.environ))
                    pinned.assert_not_called();self.assertFalse(output.exists())

    def test_untrusted_shared_daemon_never_imports_niks3_paths(self):
        # The shared daemon's untrusted listener does not trust the Niks3 key
        # (any write-token holder could otherwise plant fleet outputs), so the
        # Darwin finish publishes without a retention pin and never copies.
        import os,tempfile
        from contextlib import ExitStack
        c=self.channel
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
            output=Path(temporary)/'output'
            stack.enter_context(patch.dict(os.environ,{'GITHUB_OUTPUT':str(output),'NIX_SHARED_DAEMON':'1'}))
            stack.enter_context(patch.object(c,'module',return_value=remote))
            stack.enter_context(patch.object(remote,'verify',return_value=self.result))
            stack.enter_context(patch.object(remote,'verify_closure'))
            stack.enter_context(patch.object(c,'evaluated_roots',return_value=['signed-root']))
            stack.enter_context(patch.object(c,'remote_closure',return_value={}))
            stack.enter_context(patch.object(c,'verify_published',return_value='b'*64))
            stack.enter_context(patch.object(c,'source',return_value=self.sha))
            run=stack.enter_context(patch.object(c,'run'))
            pinned=stack.enter_context(patch.object(c,'pin_release'))
            c.finish_remote_build(self.rules,'aarch64-darwin',self.sha,'signed',dict(os.environ))
            run.assert_not_called();pinned.assert_not_called()
            text=output.read_text()
            self.assertIn('darwin=signed',text);self.assertIn('\nretention=\n',text)
        # Retained archives would need the same import: refused before any run.
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
            root=Path(temporary);(root/'ci').mkdir()
            path='/nix/store/'+'a'*32+'-archive'
            (root/'ci/retained-archives.json').write_text(json.dumps({'version':1,'releases':{self.sha:{'aarch64-darwin':path}}}))
            stack.enter_context(patch.object(c,'ROOT',root))
            stack.enter_context(patch.dict(os.environ,{'NIX_SHARED_DAEMON':'1'}))
            run=stack.enter_context(patch.object(c,'run'))
            with self.assertRaisesRegex(ValueError,'untrusted shared daemon'):
                c.retained_archive(self.rules,self.sha,'aarch64-darwin',{},[],{})
            run.assert_not_called()

    def test_promote_rejects_missing_or_malformed_proof_before_any_forge_action(self):
        import os,tempfile
        c=self.channel
        for content in (None,'invalid-json','123','"invalid-base64"'):
            with self.subTest(content=content),tempfile.TemporaryDirectory() as temporary, \
                 patch.object(c,'policy',return_value=self.rules),patch.object(c,'module',return_value=remote), \
                 patch.object(c,'run') as run,patch.dict(os.environ,{'PRIVATE_DARWIN_PROOF_DIRECTORY':temporary}):
                if content is not None:(Path(temporary)/'aarch64-darwin.json').write_text(content)
                with self.assertRaises((ValueError,KeyError)):
                    c.promote('tested',self.sha,{'aarch64-darwin':'b'*64})
                run.assert_not_called()

    def test_promote_strips_channel_token_and_requires_locally_evaluated_signed_roots(self):
        import os,tempfile
        c=self.channel
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{
            'PRIVATE_DARWIN_PROOF_DIRECTORY':temporary,'CHANNEL_TOKEN':'sentinel-writer'}), \
             patch.object(c,'policy',return_value=self.rules),patch.object(c,'module',return_value=remote), \
             patch.object(remote,'verify',return_value=self.result),patch.object(remote,'verify_closure') as verify, \
             patch.object(c,'evaluated_roots',return_value=['unsigned-root']) as evaluate,patch.object(c,'run') as run:
            (Path(temporary)/'aarch64-darwin.json').write_text('"signed"')
            with self.assertRaises(ValueError):c.promote('tested',self.sha,{'aarch64-darwin':'b'*64})
            self.assertNotIn('CHANNEL_TOKEN',evaluate.call_args.kwargs['env']);run.assert_not_called();verify.assert_not_called()

    def test_successful_worker_result_survives_cancel_transport_failure(self):
        import os,time
        item={'drv':'/nix/store/'+'a'*32+'-x.drv','path':'/nix/store/'+'b'*32+'-x','output':'out'}
        root='root-'+remote.p.digest(item['drv']+'^out')[:24]
        manifest=dict(commit='a'*40,tree='b'*40,lockHash='c'*64,system='aarch64-darwin',roots={root:item})
        policy={'repository':'d/tool','privateDarwin':{'attestorPublicKeys':['key']}}
        result=dict(id='1-1-darwin',manifestHash=remote.p.digest(manifest),issuedAt=int(time.time()),status='built')
        with patch.dict(os.environ,{'PRIVATE_DARWIN_HMAC_KEY':'1'*64,'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}), \
             patch.object(remote,'load',return_value=SimpleNamespace(evaluate=lambda *a,**k:manifest)), \
             patch.object(remote,'exchange',side_effect=[None,OSError('cancel-unavailable')]), \
             patch.object(remote.guard,'directory',return_value=1),patch.object(remote.guard,'snapshot',return_value=b'{"stub":true}'), \
             patch.object(remote.os,'open',return_value=1),patch.object(remote.os,'close'), \
             patch.object(remote.p,'verify_attestation',return_value=result),patch.object(remote,'verify'):
            self.assertEqual(remote.build(policy,'aarch64-darwin'),base64.b64encode(b'{"stub":true}').decode())

    def test_client_wait_ends_inside_the_job_and_cancels(self):
        import os,time,tempfile
        # The generated darwin-tools job timeout bounds the whole client.
        route={'attestorPublicKeys':['ssh-ed25519 TEST'],'clientCommit':'a'*40,'providerCommit':'b'*40}
        with tempfile.TemporaryDirectory() as directory:
            job=workflow_fixture.ChannelWorkflowRefs().generated('forgejo',directory,systems=('aarch64-darwin',),
                extra={'privateDarwin':route,'forgejoSharedDaemon':True})['release-channels']['jobs']['build_aarch64_darwin']
        self.assertEqual(remote.JOB_TIMEOUT_SECONDS,int(job['timeout-minutes'])*60)
        self.assertLessEqual(remote.TOOL_WAIT_SECONDS+30*60,remote.JOB_TIMEOUT_SECONDS)
        self.assertLess(remote.TOOL_WAIT_SECONDS,remote.p.CONTROLLER_WAIT_SECONDS)
        item={'drv':'/nix/store/'+'a'*32+'-x.drv','path':'/nix/store/'+'b'*32+'-x','output':'out'}
        manifest=dict(commit='a'*40,tree='b'*40,lockHash='c'*64,system='aarch64-darwin',
                      roots={'root-'+remote.p.digest(item['drv']+'^out')[:24]:item})
        policy={'repository':'d/tool','privateDarwin':{'attestorPublicKeys':['key']}}
        clock=[1000.0]
        def monotonic():
            clock[0]+=600;return clock[0]
        queued=lambda *a,**k:dict(id='1-1-darwin',manifestHash=remote.p.digest(manifest),issuedAt=int(time.time()),status='queued')
        with patch.dict(os.environ,{'PRIVATE_DARWIN_HMAC_KEY':'1'*64,'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}), \
             patch.object(remote,'load',return_value=SimpleNamespace(evaluate=lambda *a,**k:manifest)), \
             patch.object(remote,'exchange') as exchange, \
             patch.object(remote.guard,'directory',return_value=1),patch.object(remote.guard,'snapshot',return_value=b'{"stub":true}'), \
             patch.object(remote.os,'open',return_value=1),patch.object(remote.os,'close'), \
             patch.object(remote.p,'verify_attestation',side_effect=queued), \
             patch.object(remote.time,'monotonic',side_effect=monotonic),patch.object(remote.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'Worker deadline exceeded'):remote.build(policy,'aarch64-darwin')
        # Started at 1600; every poll advances 10 min; the wait never passes the bound.
        self.assertLessEqual(clock[0]-1600-600,remote.TOOL_WAIT_SECONDS)
        self.assertEqual(exchange.call_args_list[-1].args[1]['action'],'cancel')

class SharedDaemonJobs(unittest.TestCase):
    def test_default_generation_keeps_existing_containers_and_helpers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            workflows=workflow_fixture.ChannelWorkflowRefs().generated('forgejo',temporary,systems=('x86_64-linux','aarch64-darwin'))
            installed=json.loads((Path(temporary)/'ci/channel-tool-hashes.json').read_text())
            self.assertFalse((Path(temporary)/'ci/connect-shared-daemon.sh').exists())
            self.assertFalse(any((Path(temporary)/'ci'/name).exists() for name in workflow_fixture.installer.PRIVATE_DARWIN_HELPERS))
        self.assertEqual(list(installed),workflow_fixture.installer.HELPERS)
        text=json.dumps(workflows)
        for forbidden in ('flakey-ci-nix-pr','connect-shared-daemon','NIX_SHARED_DAEMON','darwin-tools','darwin-tool-metadata'):
            self.assertNotIn(forbidden,text)
        for workflow in workflows.values():
            for job in workflow['jobs'].values():
                if job['runs-on'] in ('nix','nix-light'):
                    self.assertEqual(job['container'],{'image':'nixos/nix@sha256:7a007c766426c1877758ddc5cb87a965ac131fc78c582ce0083d922d51ae945c'})
                    self.assertNotIn('NIX_REMOTE',job['env'])
        # The Darwin route without the opt-in keeps its existing native label.
        self.assertEqual(workflows['release-channels']['jobs']['build_aarch64_darwin']['runs-on'],'darwin-aarch64')

    def test_shared_daemon_opt_in_rejects_other_images_and_retained_archives(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary,self.assertRaisesRegex(SystemExit,'shared daemon image'):
            workflow_fixture.ChannelWorkflowRefs().generated('forgejo',temporary,
                extra={'forgejoSharedDaemon':True,'forgejoDeterminateImage':'sha256:'+'b'*64,'forgejoDeterminateLabels':['nix']})
        with tempfile.TemporaryDirectory() as temporary:
            workflow_fixture.ChannelWorkflowRefs().generated('forgejo',temporary,
                extra={'forgejoSharedDaemon':True,'forgejoDeterminateImage':workflow_fixture.installer.SHARED_DAEMON_IMAGE,'forgejoDeterminateLabels':['nix']})
        with tempfile.TemporaryDirectory() as temporary,self.assertRaisesRegex(SystemExit,'retained archives'):
            (Path(temporary)/'ci').mkdir();(Path(temporary)/'ci/retained-archives.json').write_text('{}')
            workflow_fixture.ChannelWorkflowRefs().generated('forgejo',temporary,extra={'forgejoSharedDaemon':True})

    def test_private_darwin_and_darwin_tools_require_the_explicit_opt_in(self):
        import tempfile
        route={'attestorPublicKeys':['ssh-ed25519 TEST'],'clientCommit':'a'*40,'providerCommit':'b'*40}
        for extra in ({'privateDarwin':route},
                      {'forgejoDeterminateImage':'sha256:'+'a'*64,'forgejoDeterminateLabels':['darwin-tools']},
                      {'forgejoSharedDaemon':'yes'}):
            with self.subTest(extra=extra),tempfile.TemporaryDirectory() as temporary,self.assertRaises(SystemExit):
                workflow_fixture.ChannelWorkflowRefs().generated('forgejo',temporary,extra=extra)
        with tempfile.TemporaryDirectory() as temporary,self.assertRaises(SystemExit):
            workflow_fixture.ChannelWorkflowRefs().generated('github',temporary,extra={'forgejoSharedDaemon':True})

    def test_every_icicle_job_mounts_shared_store_readonly_and_never_starts_a_daemon(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            workflows=workflow_fixture.ChannelWorkflowRefs().generated('forgejo',temporary,systems=('x86_64-linux',),
                extra={'forgejoSharedDaemon':True})
            installed=json.loads((Path(temporary)/'ci/channel-tool-hashes.json').read_text())
        self.assertEqual(list(installed),workflow_fixture.installer.HELPERS+workflow_fixture.installer.SHARED_DAEMON_HELPERS)
        labels=set()
        for workflow in workflows.values():
            for job in workflow['jobs'].values():
                if job['runs-on'] not in ('nix','nix-light','darwin-tools'):continue
                labels.add(job['runs-on'])
                volumes=job['container']['volumes']
                self.assertIn('flakey-ci-nix-pr-v2-ro:/nix:ro',volumes)
                self.assertIn('flakey-ci-nix-pr-socket-untrusted-ro:/nix/var/nix/daemon-socket:ro',volumes)
                # Never the store volume (any mode), trusted socket or creds tmpfs.
                self.assertFalse([v for v in volumes if v.split(':',1)[0] in
                                  ('flakey-ci-nix-pr-v2','flakey-ci-nix-pr-socket','flakey-ci-nix-pr-socket-untrusted','flakey-ci-nix-pr-creds')])
                self.assertNotIn('nix-daemon',str(job['steps']))
                self.assertTrue(any('connect-shared-daemon.sh' in step.get('run','') for step in job['steps']))
        self.assertEqual(labels,{'nix','nix-light'})

class SharedDaemonFailure(unittest.TestCase):
    def test_unavailable_shared_daemon_never_starts_a_private_replacement(self):
        import os
        channel=ReleaseBindings();channel.setUp();c=channel.channel
        with patch.dict(os.environ,{'NIX_REMOTE':'daemon','NIX_SHARED_DAEMON':'1'}), \
             patch.object(c,'daemon_healthy',return_value=False),patch.object(c.subprocess,'Popen') as spawn:
            with self.assertRaises(c.CommandFailure):c.ensure_daemon()
            spawn.assert_not_called()


class UntrustedConnector(unittest.TestCase):
    def test_connector_accepts_only_an_untrusted_matching_daemon(self):
        import os,subprocess,tempfile
        for trusted,ok in (('false',True),('true',False)):
            with self.subTest(trusted=trusted),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);bin=root/'bin';bin.mkdir()
                (bin/'nix').write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo "nix (Determinate Nix 3.24.1) 2.33.1"; exit 0; fi\n'
                    'echo \'{"url":"daemon","version":"2.33.1","trusted":'+trusted+'}\'\n')
                (bin/'nix').chmod(0o755)
                env=dict(os.environ,PATH=str(bin)+':'+os.environ['PATH'],GITHUB_ENV=str(root/'env'),NIX_DAEMON_CONNECT_WAIT='1')
                script=(ROOT/'ci/connect-shared-daemon.sh').read_text().replace('[[ -S "$socket" ]] && ','')
                result=subprocess.run(['bash','-c',script],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode==0,ok,result.stderr)
                if ok:
                    written=(root/'env').read_text()
                    self.assertIn('NIX_REMOTE=daemon',written)
                    self.assertNotIn('NIX_JOB_CREDENTIALS',written)
