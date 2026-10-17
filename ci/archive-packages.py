#!/usr/bin/env python3
"""Copy package artifact ZIPs to release assets as data; never run their contents."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
NAME = re.compile(r'(?:nix-|fork-cache-)[A-Za-z0-9._+-]+-[0-9a-f]{40}')
PART_SIZE = 1900 * 1024 * 1024
MAX_ARCHIVE = 5 * 1024 * 1024 * 1024


def gh(*args):
    return subprocess.check_output(['gh', *args])


def validate_run(data, repository, branch):
    if (data.get('repository', {}).get('full_name') != repository
            or data.get('head_repository', {}).get('full_name') != repository
            or data.get('event') not in ('push', 'workflow_dispatch')
            or data.get('head_branch') != branch
            or not re.fullmatch('[0-9a-f]{40}', data.get('head_sha', ''))):
        raise ValueError('Only trusted default-branch push/manual runs may publish backups')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def create_release(repository, branch, run, tag, artifacts):
    # GitHub requires workflow-write authority to tag an older commit whose
    # workflows differ from the current default branch. Backup tags identify
    # archive publication; the run and asset names identify the built source.
    revisions = sorted({item['name'].rsplit('-', 1)[1] for item in artifacts})
    gh('release', 'create', tag, '--repo', repository, '--target', branch, '--prerelease',
       '--title', 'Package backups: ' + revisions[0][:12] + ' (run ' + str(run['id']) + ')',
       '--notes', 'Nix runtime-closure archives from ' + run['html_url'] + '. Built source revisions: ' + ', '.join(revisions)
       + '. Workflow trigger revision: ' + run['head_sha']
       + '. The backup tag marks the default branch at archive publication, not the built source revision. '
       'Successful package outputs are retained even if another build or Attic upload fails. Each ZIP contains '
       'a gzip Nix export, output roots and checksums. These are data archives, not installers.')


def chunks(path, size=PART_SIZE):
    if path.stat().st_size <= size:
        yield path
        return
    with path.open('rb') as source:
        index = 0
        while True:
            part = path.with_name(path.name + '.part-' + str(index).zfill(4))
            with part.open('wb') as target:
                written = 0
                while written < size:
                    block = source.read(min(1024 * 1024, size - written))
                    if not block:
                        break
                    target.write(block)
                    written += len(block)
            if not written:
                part.unlink()
                break
            try:
                yield part
            finally:
                part.unlink(missing_ok=True)
            index += 1


def main():
    repository = os.environ['GITHUB_REPOSITORY']
    branch = os.environ['DEFAULT_BRANCH']
    run_id = os.environ['PACKAGE_RUN_ID']
    if not REPO.fullmatch(repository) or not re.fullmatch('[0-9]{1,20}', run_id):
        raise ValueError('Invalid repository or workflow run')
    run = json.loads(gh('api', f'repos/{repository}/actions/runs/{run_id}'))
    validate_run(run, repository, branch)
    pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100'))
    artifacts = [item for page in pages for item in page['artifacts'] if NAME.fullmatch(item['name']) and not item['expired']]
    if not artifacts:
        print('No successful package artifacts were produced by this run.')
        return
    tag = 'packages-' + run_id
    if subprocess.run(['gh', 'release', 'view', tag, '--repo', repository], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        create_release(repository, branch, run, tag, artifacts)
    for item in artifacts:
        if not 0 < item['size_in_bytes'] <= MAX_ARCHIVE:
            raise ValueError('Package archive exceeds the transfer limit')
        with tempfile.TemporaryDirectory(prefix='package-backup-') as temporary:
            path = Path(temporary) / (item['name'] + '.zip')
            with path.open('wb') as target:
                subprocess.run(['gh', 'api', '--allow-escape-sequences', f'repos/{repository}/actions/artifacts/{item["id"]}/zip'], stdout=target, check=True)
            if path.stat().st_size != item['size_in_bytes']:
                raise ValueError('Downloaded archive size does not match GitHub metadata')
            checksum = digest(path)
            uploaded = []
            for part in chunks(path):
                part_digest = digest(part)
                gh('release', 'upload', tag, str(part), '--repo', repository, '--clobber')
                release = json.loads(gh('api', f'repos/{repository}/releases/tags/{tag}'))
                asset = next(a for a in release['assets'] if a['name'] == part.name)
                if asset['size'] != part.stat().st_size or (asset.get('digest') and asset['digest'] != 'sha256:' + part_digest):
                    raise ValueError('Release asset verification failed')
                uploaded.append(part.name)
            sums = path.with_name(path.name + '.sha256')
            sums.write_text(checksum + '  ' + path.name + '\n')
            gh('release', 'upload', tag, str(sums), '--repo', repository, '--clobber')
            print('Backed up ' + item['name'] + ' as ' + ', '.join(uploaded))
            # Remove only the now-verified duplicate artifact to conserve Actions storage.
            gh('api', '--method', 'DELETE', f'repos/{repository}/actions/artifacts/{item["id"]}')


if __name__ == '__main__':
    main()
