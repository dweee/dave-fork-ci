#!/usr/bin/env python3
"""Snapshot untrusted requests and atomically answer through pinned directory FDs.

The shell broker only processes private snapshots, never job-writable paths.
No symlink or hardlink in an admitted store is followed, including ancestors.
"""
import os
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

ID = re.compile(r'[0-9]+-[0-9]+(?:-[a-z0-9]{1,16})?')
LIMIT = 2 * 1024 * 1024


def directory(path):
    if not Path(path).is_absolute():
        raise ValueError('Absolute spool required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            if part in {'.', '..'}:
                raise ValueError('Invalid spool component')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def snapshot(fd, name):
    # O_PATH inspects an inode without opening a device (e.g. watchdog/TTY).
    # Reopening the pinned regular inode through procfs avoids a lstat/open race.
    handle = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT:
            raise ValueError('Unsafe or excessive request')
        file = os.open('/proc/self/fd/' + str(handle), os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        current = os.fstat(file)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            os.close(file)
            raise ValueError('Request inode changed')
    finally:
        os.close(handle)
    try:
        data = bytearray()
        while len(data) <= LIMIT:
            chunk = os.read(file, min(65536, LIMIT + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise ValueError('Excessive request')
    finally:
        os.close(file)


def publish(fd, name, data):
    # An unpredictable exclusive temporary cannot be replaced by a symlink
    # between checking and opening. replace/unlink never follow the destination.
    temporary = '.guard-' + uuid.uuid4().hex
    file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
    try:
        with os.fdopen(file, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def run(helper, spools, state):
    work = state / 'guard'
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    if work.is_symlink() or work.stat().st_uid != os.getuid() or stat.S_IMODE(work.stat().st_mode) != 0o700:
        raise ValueError('Unsafe private guard state')
    for entry in spools.split():
        name, path = entry.split('=', 1)
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,15}', name):
            raise ValueError('Invalid admitted spool name')
        fds = []
        try:
            base = directory(path)
            fds.append(base)
            requests = os.open('requests', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
            fds.append(requests)
            results = os.open('results', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
            fds.append(results)
            for filename in sorted(os.listdir(requests))[:256]:
                if not filename.endswith('.json') or filename.startswith('.'):
                    continue
                request_id = filename[:-5]
                if not ID.fullmatch(request_id):
                    # unlink does not follow symlinks; never recurse into a job directory.
                    try:
                        os.unlink(filename, dir_fd=requests)
                    except OSError:
                        pass
                    continue
                try:
                    data = snapshot(requests, filename)
                    os.unlink(filename, dir_fd=requests)
                except (OSError, ValueError):
                    # Reject without reading through a planted link or touching its target.
                    publish(results, filename, json.dumps({'version': 2, 'id': request_id,
                        'status': 'rejected', 'detail': 'unsafe request file', 'targets': {}, 'sessions': {}}).encode())
                    try:
                        os.unlink(filename, dir_fd=requests)
                    except OSError:
                        pass
                    continue
                with tempfile.TemporaryDirectory(prefix=name + '-', dir=work) as temporary:
                    private = Path(temporary)
                    for child in ['requests', 'active', 'results']:
                        (private / child).mkdir()
                    (private / 'requests' / filename).write_bytes(data)
                    env = {**os.environ, 'REMOTE_BUILD_PRIVATE_SPOOL': '1',
                           'REMOTE_BUILD_SPOOLS': name + '=' + str(private)}
                    try:
                        subprocess.run(['bash', str(helper)], env=env, check=True)
                        answer = private / 'results' / filename
                        if not answer.is_file() or answer.stat().st_size > LIMIT:
                            raise ValueError('Missing bounded broker response')
                        response = answer.read_bytes()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        response = json.dumps({'version': 2, 'id': request_id,
                            'status': 'failed', 'detail': 'helper error', 'targets': {}, 'sessions': {}}).encode()
                    publish(results, filename, response)
        except (OSError, ValueError):
            print('Refused unsafe or unavailable admitted spool: ' + name, file=sys.stderr)
        finally:
            for fd in reversed(fds):
                os.close(fd)


if __name__ == '__main__':
    run(Path(sys.argv[1]), os.environ['REMOTE_BUILD_SPOOLS'],
        Path(os.environ.get('REMOTE_BUILD_STATE', '/var/lib/flakey-remote-build')))
