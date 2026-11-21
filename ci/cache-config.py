#!/usr/bin/env python3
"""Render the reviewed public cache policy; private Niks3 is appended by its reader."""
import json
from pathlib import Path

POLICY = json.loads(Path(__file__).with_name('caches.json').read_text())
PUBLIC = [url for url in POLICY['substituters'] if url != 'https://niks3.monke.fail']
KEYS = POLICY['trusted-public-keys']

def nix_config():
    return ('substituters = ' + ' '.join(PUBLIC) + '\n'
            + 'trusted-public-keys = ' + ' '.join(KEYS) + '\n'
            + 'extra-trusted-substituters = ' + ' '.join(POLICY['extra-trusted-substituters']) + '\n')

if __name__ == '__main__':
    print(nix_config(), end='')
