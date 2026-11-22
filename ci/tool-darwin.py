#!/usr/bin/env python3
"""Authenticated tool build client and independent promote-time verifier."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

CODE = Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), CODE/(name+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

p, guard = load('tool-darwin-protocol'), load('tool-darwin-spool')

# The generated darwin-tools job and its runner both stop at six hours from job
# start. Give up waiting early enough that the cancellation in build()'s
# finally clause, result verification and pinning still run inside the job:
# 45 minutes covers checkout, connector, evaluation and the post-result steps.
JOB_TIMEOUT_SECONDS = 360*60
TOOL_WAIT_SECONDS = JOB_TIMEOUT_SECONDS - 45*60

def verify(encoded, policy, revision, system):
    route = policy['privateDarwin']
    envelope = json.loads(base64.b64decode(encoded, validate=True))
    result = p.verify_attestation(envelope, route['attestorPublicKeys'])
    manifest = result.get('manifest', {})
    if (result.get('status') != 'built' or result.get('action') != 'result'
            or manifest.get('repository') != policy['repository'] or manifest.get('commit') != revision
            or manifest.get('system') != system or manifest.get('policyHash') != p.digest(policy)
            or result.get('manifestHash') != p.digest(manifest)
            or not manifest.get('roots') or result.get('targets') != dict.fromkeys(manifest['roots'], 'built')
            or result.get('executor') != (p.INTEL_EXECUTOR if system == 'x86_64-darwin' else p.HOSTED_EXECUTOR) or len(result.get('sessions', [])) != 1
            or result.get('client') != route['clientCommit'] or result.get('provider') != route['providerCommit']
            or not p.matches(p.HASH, result.get('closureHash'))):
        raise ValueError('Remote Darwin result differs from release selection')
    return result

def verification_environment(env):
    allowed={'PATH','HOME','TMPDIR','LANG','LC_ALL','NIX_REMOTE','NIX_CONFIG','SSL_CERT_FILE','NIX_SSL_CERT_FILE',
             'XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_STATE_HOME','NIX_USER_CONF_FILES','NIKS3_NETRC_FILE','NIX_JOB_CREDENTIALS'}
    return {k:v for k,v in env.items() if k in allowed}


def verify_roots(result, roots):
    if not set(roots) <= {r['path'] for r in result['manifest']['roots'].values()}:
        raise ValueError('Local release roots are absent from the signed manifest')


def verify_closure(result, release, env):
    env=verification_environment(env)
    roots = sorted({r['path'] for r in result['manifest']['roots'].values()})
    measured = release.remote_closure(roots, env)
    metadata = {path:release.metadata(entry) for path,entry in measured.items()}
    if p.digest(metadata) != result['closureHash']:
        raise ValueError('Worker and published selected closures differ')
    # The ordinary cache gate can adopt a pre-existing realisation. Require the
    # exact same metadata snapshot again so adoption cannot weaken this binding.
    proof = release.verify_published(roots, measured, env)
    if proof != release.digest({'roots':roots, 'metadata':metadata}):
        raise ValueError('Published selected closure changed during verification')
    return metadata


def exchange(spool, body, key):
    base = guard.directory(str(spool))
    try:
        requests = os.open('requests', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
        try:
            previous = os.umask(0o022)
            try:
                guard.publish(requests, body['id']+('-cancel' if body['action']=='cancel' else '')+'.json', p.canonical(p.sign(body,key)))
            finally: os.umask(previous)
        finally: os.close(requests)
    finally: os.close(base)

def build(policy, system):
    started = time.monotonic()
    manifest = load('tool-darwin-evaluate').evaluate(CODE.parent, full=False, system=system)
    now = int(time.time())
    source = {k:manifest[k] for k in ('commit','tree','lockHash')}
    source.update(repository=policy['repository'], mode='main', prNumber=0, head=source['commit'], base=source['commit'])
    body = dict(version=p.VERSION,kind=p.KIND,action='build',id=os.environ['GITHUB_RUN_ID']+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1')+'-darwin',
        nonce=secrets.token_hex(32),issuedAt=now,expiresAt=now+600,source=source,manifest=manifest,manifestHash=p.digest(manifest))
    p.validate_request(body, policy['repository'], system)
    spool = Path('/run/darwin-tool-metadata')/system
    key = os.environ['PRIVATE_DARWIN_HMAC_KEY']
    exchange(spool,body,key)
    lease = now
    # Never wait past the job's own deadline: a killed job skips the cancel.
    deadline = started + min(p.CONTROLLER_WAIT_SECONDS, TOOL_WAIT_SECONDS)
    try:
        while time.monotonic() < deadline:
            base = guard.directory(str(spool))
            try:
                results = os.open('results', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
                try:
                    try: envelope = json.loads(guard.snapshot(results, body['id']+'.json'))
                    except (OSError,ValueError): envelope = None
                finally: os.close(results)
            finally: os.close(base)
            if envelope:
                try:
                    result = p.verify_attestation(envelope,policy['privateDarwin']['attestorPublicKeys'],int(time.time()))
                    if result.get('id') != body['id'] or result.get('manifestHash') != body['manifestHash']:
                        raise ValueError('Wrong request')
                except (ValueError,KeyError,TypeError): result = None
                if result:
                    lease = result['issuedAt']
                    if result['status'] == 'built':
                        encoded = base64.b64encode(p.canonical(envelope)).decode()
                        verify(encoded,policy,source['commit'],system)
                        return encoded
                    if result['status'] not in ('queued','running'): raise ValueError('Worker failed')
            if time.time()-lease > 300: raise ValueError('Worker lease expired')
            time.sleep(10)
        raise ValueError('Worker deadline exceeded')
    finally:
        cancel = {k:body[k] for k in ('version','kind','id','manifestHash')}
        cancel.update(action='cancel',nonce=secrets.token_hex(32),issuedAt=int(time.time()),expiresAt=int(time.time())+600)
        try:
            exchange(spool,cancel,key)
        except Exception:
            print('Private Darwin cancellation could not be delivered',file=sys.stderr)
