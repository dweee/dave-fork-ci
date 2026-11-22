#!/usr/bin/env python3
"""Authenticated metadata for the separately admitted Darwin build spool.

This protocol conveys no credential or source archive. It is deliberately
incompatible with the proven-public broker's v2 offers.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import threading
from pathlib import Path
import subprocess
import tempfile

VERSION = 3
CONTROLLER_WAIT_SECONDS = 21600
KIND = 'private-darwin-coverage'
# A hosted Mac session ran. Cached means every selected root and its whole
# runtime closure were already signed in the fleet cache, so no Mac started.
HOSTED_EXECUTOR = 'GitHub-hosted aarch64-darwin'
CACHE_EXECUTOR = 'Niks3 signed cache (aarch64-darwin)'
INTEL_EXECUTOR = 'GitHub-hosted aarch64-darwin (x86_64-darwin via Rosetta)'
EXECUTORS = (HOSTED_EXECUTOR, CACHE_EXECUTOR, INTEL_EXECUTOR)
TICKET = re.compile(r'[1-9][0-9]*-[1-9][0-9]*-darwin')
ROOT_ID = re.compile(r'root-[a-f0-9]{24}')
SHA = re.compile(r'[a-f0-9]{40}')
HASH = re.compile(r'[a-f0-9]{64}')
STORE = re.compile(r'/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+._?=-]+')
OUTPUT = re.compile(r'[A-Za-z_][A-Za-z0-9_-]*')


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def signing_key(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError('A dedicated 256-bit signing key is required')
    return bytes.fromhex(value)


def sign(body, key):
    return {'body': body, 'mac': hmac.new(signing_key(key), canonical(body), hashlib.sha256).hexdigest()}


def authenticate(envelope, key, now):
    if not isinstance(envelope, dict) or set(envelope) != {'body', 'mac'}:
        raise ValueError('Invalid private envelope')
    body, mac = envelope['body'], envelope['mac']
    if (not isinstance(body, dict) or not isinstance(mac, str) or not HASH.fullmatch(mac)
            or not hmac.compare_digest(sign(body, key)['mac'], mac)):
        raise ValueError('Private envelope authentication failed')
    validate_time(body, now)
    return body


def validate_time(body, now):
    issued, expires = body.get('issuedAt'), body.get('expiresAt')
    if (type(issued) is not int or type(expires) is not int or issued > now + 120
            or expires <= now or not 0 < expires-issued <= 86400
            or body.get('version') != VERSION or body.get('kind') != KIND
            or not TICKET.fullmatch(str(body.get('id', '')))):
        raise ValueError('Invalid or expired private envelope')


def attest(body, key_path):
    """Worker-only SSH Ed25519 signature; the request HMAC cannot forge it."""
    signature = subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-f', str(key_path),
        '-n', 'flakey-darwin-coverage'], input=canonical(body), capture_output=True, check=True).stdout.decode()
    return {'body': body, 'signature': signature}


def verify_attestation(envelope, keys, now=None):
    if (not isinstance(envelope, dict) or set(envelope) != {'body', 'signature'}
            or not isinstance(envelope['body'], dict) or not isinstance(envelope['signature'], str)
            or len(envelope['signature']) > 4096 or not isinstance(keys, list) or not keys
            or any(not isinstance(key, str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+', key) for key in keys)):
        raise ValueError('Invalid worker attestation or unadmitted public key')
    body = envelope['body']
    # Immutable bundles retain validity after the live lease expires. Their
    # commit/manifest/closure identities are checked again by the consumer.
    validate_time(body, body.get('issuedAt', 0) if now is None else now)
    with tempfile.TemporaryDirectory(prefix='darwin-attestation-') as temporary:
        allowed, signature = Path(temporary)/'allowed', Path(temporary)/'signature'
        allowed.write_text(''.join('flakey-darwin-worker '+key+'\n' for key in keys))
        signature.write_text(envelope['signature'])
        result = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(allowed),
            '-I', 'flakey-darwin-worker', '-n', 'flakey-darwin-coverage', '-s', str(signature)],
            input=canonical(body), capture_output=True)
        if result.returncode:
            raise ValueError('Worker attestation verification failed')
    return body


def validate_request(body, repository="d/flakey", system="aarch64-darwin"):
    required = {'version', 'kind', 'action', 'id', 'nonce', 'issuedAt', 'expiresAt', 'manifestHash'}
    action = body.get('action')
    if action == 'build':
        required |= {'source', 'manifest'}
        # Optional: the controller accepts a cache-verified result. Absent, the
        # worker always runs a hosted session as before.
        if 'acceptsCacheVerified' in body:
            if body['acceptsCacheVerified'] is not True:
                raise ValueError('Invalid private request')
            required |= {'acceptsCacheVerified'}
    elif action != 'cancel':
        raise ValueError('Invalid private request action')
    if (set(body) != required or not matches(HASH, body['nonce'])
            or not matches(HASH, body['manifestHash'])
            or body['expiresAt']-body['issuedAt'] > 600):
        raise ValueError('Invalid private request')
    if action == 'cancel':
        return body
    source, manifest = body['source'], body['manifest']
    if (not isinstance(source, dict) or set(source) != {
            'mode', 'repository', 'prNumber', 'head', 'base', 'commit', 'tree', 'lockHash'}
            or source['repository'] != repository or not isinstance(source['mode'], str) or source['mode'] not in {'pr', 'main'}
            or type(source['prNumber']) is not int
            or (source['prNumber'] <= 0 if source['mode'] == 'pr' else source['prNumber'] != 0)
            or any(not matches(SHA, source[k]) for k in ['head', 'base', 'commit', 'tree'])
            or not matches(HASH, source['lockHash'])
            or source['mode'] == 'main' and len({source['head'], source['base'], source['commit']}) != 1):
        raise ValueError('Invalid private source identity')
    if (not isinstance(manifest, dict) or manifest.get('system') != system
            or any(manifest.get(k) != source[k] for k in ['commit', 'tree', 'lockHash'])
            or digest(manifest) != body['manifestHash']
            or not isinstance(manifest.get('roots'), dict) or not 1 <= len(manifest['roots']) <= 5000):
        raise ValueError('Invalid private manifest identity')
    for name, item in manifest['roots'].items():
        if (not ROOT_ID.fullmatch(name) or not isinstance(item, dict)
                or set(item) != {'drv', 'output', 'path'}
                or not matches(STORE, item['drv']) or not item['drv'].endswith('.drv')
                or not matches(STORE, item['path']) or item['path'].endswith('.drv')
                or not matches(OUTPUT, item['output'])
                or name != 'root-'+digest(item['drv']+'^'+item['output'])[:24]):
            raise ValueError('Invalid private root identity')
    return body


class Nonces:
    """Host-owned persistent replay protection; no state comes from the spool."""
    def __init__(self, path):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute('CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS cancellations (ticket TEXT, manifest TEXT, PRIMARY KEY(ticket, manifest))')
        self.connection.execute('CREATE TABLE IF NOT EXISTS queue (ticket TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def claim(self, nonce):
        try:
            with self.lock, self.connection:
                self.connection.execute('INSERT INTO nonces VALUES (?)', (nonce,))
        except sqlite3.IntegrityError:
            raise ValueError('Replayed private request') from None

    def close(self):
        self.connection.close()

    def cancel(self, ticket, manifest):
        with self.lock, self.connection:
            self.connection.execute('INSERT OR IGNORE INTO cancellations VALUES (?, ?)', (ticket, manifest))

    def cancelled(self, ticket, manifest):
        with self.lock:
            return self.connection.execute('SELECT 1 FROM cancellations WHERE ticket=? AND manifest=?',
                                           (ticket, manifest)).fetchone() is not None

    def enqueue(self, body):
        with self.lock, self.connection:
            self.connection.execute('INSERT INTO queue VALUES (?, ?)', (body['id'], canonical(body).decode()))

    def pending(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.connection.execute('SELECT body FROM queue ORDER BY rowid')]

    def dequeue(self, ticket):
        with self.lock, self.connection:
            self.connection.execute('DELETE FROM queue WHERE ticket=?', (ticket,))


def result(body, status, targets, sessions, client, provider, now, closure_hash=None, executor=HOSTED_EXECUTOR):
    if (executor not in EXECUTORS
            or executor == CACHE_EXECUTOR and (sessions or status != 'built'
                                               or body.get('acceptsCacheVerified') is not True)
            or status not in {'queued', 'running', 'built', 'failed', 'cancelled', 'rejected'}
            or not all(ROOT_ID.fullmatch(name) and value in {'built', 'failed'}
                       for name, value in targets.items())
            or not all(re.fullmatch(r'b[a-z0-9]{6}', session) for session in sessions)
            or not SHA.fullmatch(client) or not SHA.fullmatch(provider)
            or status == 'built' and not matches(HASH, closure_hash)):
        raise ValueError('Invalid private result')
    return {'version': VERSION, 'kind': KIND, 'action': 'result', 'id': body['id'],
            'manifestHash': body['manifestHash'], 'status': status, 'targets': targets,
            'sessions': sessions, 'client': client, 'provider': provider,
            'closureHash': closure_hash,
            'executor': executor, 'issuedAt': now, 'expiresAt': now+86400}
