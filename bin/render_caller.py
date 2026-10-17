#!/usr/bin/env python3
"""Render a pinned caller to a staging file; never enable or dispatch it."""
import argparse
from pathlib import Path
import re


def render(repository, revision):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Use owner/repo and an immutable 40-character revision')
    return '''name: Sync and test upstream
on:
  schedule:
    - cron: '17 */6 * * *'
  workflow_dispatch:
  repository_dispatch:
    types: [upstream-updated]
permissions: {}
jobs:
  upstream:
    permissions:
      contents: write
    uses: REPOSITORY/.github/workflows/upstream-sync.yml@REVISION
    with:
      tool-repository: REPOSITORY
      tool-revision: REVISION
      auto-merge: true
      cache: true
      attic-endpoint: ${{ vars.ATTIC_ENDPOINT }}
      attic-cache: ${{ vars.ATTIC_CACHE }}
      attic-substituter: ${{ vars.ATTIC_SUBSTITUTER }}
      attic-public-key: ${{ vars.ATTIC_PUBLIC_KEY }}
    secrets:
      ATTIC_TOKEN: ${{ secrets.ATTIC_TOKEN }}
'''.replace('REPOSITORY', repository).replace('REVISION', revision)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool-repository', required=True)
    parser.add_argument('--tool-revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; refusing to overwrite it')
    if '.github' in args.output.parts or '.forgejo' in args.output.parts:
        parser.error('Render outside live workflow directories for review first')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.tool_repository, args.tool_revision))
