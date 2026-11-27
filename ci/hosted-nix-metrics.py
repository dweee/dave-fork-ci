#!/usr/bin/env python3
"""Record disk headroom and elapsed time on a disposable hosted trial runner."""
import json
import os
from pathlib import Path
import shutil
import sys
import time

phase = sys.argv[1]
path = Path(os.environ['RUNNER_TEMP']) / 'hosted-nix-metrics.json'
observations = json.loads(path.read_text()) if path.exists() else []
now = time.time()
entry = {'phase': phase, 'time': now, 'elapsed_seconds': now - observations[0]['time'] if observations else 0,
         'runner_os': os.environ['RUNNER_OS'], 'runner_arch': os.environ['RUNNER_ARCH'],
         'image_os': os.environ.get('ImageOS'), 'image_version': os.environ.get('ImageVersion'),
         'mode': os.environ['TRIAL_MODE'], 'disks': {}}
for directory in ('/', '/nix', '/tmp', os.environ['GITHUB_WORKSPACE']):
    if Path(directory).exists():
        entry['disks'][directory] = dict(zip(('total', 'used', 'free'), shutil.disk_usage(directory)))
observations.append(entry)
path.write_text(json.dumps(observations, indent=2) + '\n')
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write(f"\n{phase}: {entry['elapsed_seconds']:.1f}s since trial start\n\n")
    for directory, disk in entry['disks'].items():
        summary.write(f"- `{directory}`: {disk['free'] / 1024**3:.2f} GiB available\n")
print(json.dumps(entry))
