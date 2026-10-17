"""Deterministically remove configured artifact paths without executing Git content."""
import hashlib


def filter_history(repo, revision, paths, git):
    """Return a commit with identical authors/dates/messages and a filtered DAG.

    Unaffected commits retain their IDs. Signatures on rewritten commits are
    removed because they authenticate the original objects, not the new ones.
    Raw upstream objects are never parents of the returned filtered history.
    """
    paths = tuple(sorted(set(paths)))
    if not paths:
        return revision
    commits = git(repo, 'rev-list', '--reverse', '--topo-order', revision).stdout.decode().splitlines()
    ids = git(repo, 'rev-list', '--objects', '--no-object-names', '--filter=blob:none', revision).stdout
    stream = git(repo, 'cat-file', '--batch', input_data=ids).stdout
    objects = {}
    offset = 0
    while offset < len(stream):
        end = stream.index(b'\n', offset)
        oid, kind, size = stream[offset:end].split()
        offset = end + 1
        objects[oid.decode()] = (kind, stream[offset:offset + int(size)])
        offset += int(size)
        assert stream[offset:offset + 1] == b'\n'
        offset += 1
    trees = {}

    def save(kind, body):
        oid = hashlib.sha1(kind.encode() + b' ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        actual = git(repo, 'hash-object', '-w', '-t', kind, '--stdin', input_data=body).stdout.decode().strip()
        assert oid == actual
        return oid

    def tree(oid, prefix=''):
        key = (oid, prefix)
        if key in trees:
            return trees[key]
        kind, body = objects[oid]
        assert kind == b'tree'
        entries = []
        offset = 0
        while offset < len(body):
            split = body.index(b' ', offset)
            end = body.index(b'\0', split)
            mode, name = body[offset:split], body[split + 1:end]
            child = body[end + 1:end + 21].hex()
            offset = end + 21
            path = prefix + name.decode('utf-8', errors='surrogateescape')
            if any(path == excluded or path.startswith(excluded + '/') for excluded in paths):
                continue
            if mode == b'40000':
                child = tree(child, path + '/')
            entries.append(mode + b' ' + name + b'\0' + bytes.fromhex(child))
        filtered = b''.join(entries)
        trees[key] = oid if filtered == body else save('tree', filtered)
        return trees[key]

    mapped = {}
    for oid in commits:
        kind, body = objects[oid]
        assert kind == b'commit'
        header, message = body.split(b'\n\n', 1)
        headers = []
        for line in header.splitlines():
            if line.startswith(b' '):
                headers[-1] += b'\n' + line
            else:
                headers.append(line)
        updated = []
        for line in headers:
            if line.startswith(b'tree '):
                line = b'tree ' + tree(line.split()[1].decode()).encode()
            elif line.startswith(b'parent '):
                line = b'parent ' + mapped[line.split()[1].decode()].encode()
            updated.append(line)
        if updated == headers:
            mapped[oid] = oid
            continue
        updated = [line for line in updated if not line.startswith((b'gpgsig ', b'gpgsig-sha256 ', b'mergetag '))]
        mapped[oid] = save('commit', b'\n'.join(updated) + b'\n\n' + message)
    return mapped[revision]
