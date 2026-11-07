"""Private native jobs must recheck their host's build admission policy."""
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_admission', ROOT/'ci/release-channel.py')
channel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(channel)


class NativeAdmission(unittest.TestCase):
    def test_only_private_darwin_builds_require_the_host_checker(self):
        with patch.object(channel.subprocess, 'run') as run:
            channel.native_build_ready({'forge':'github'}, 'aarch64-darwin', {})
            channel.native_build_ready({'forge':'forgejo'}, 'aarch64-linux', {})
            run.assert_not_called()

    def test_missing_or_mutable_checker_cannot_start_a_build(self):
        for value in ['', '/tmp/job/check', '/nix/store/invalid', '/nix/store/'+'a'*32+'-check --bypass']:
            with self.subTest(value=value), patch.object(channel.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    channel.native_build_ready({'forge':'forgejo'}, 'aarch64-darwin', {'NATIVE_BUILD_ADMISSION_COMMAND':value})
                run.assert_not_called()

    def test_approved_checker_is_called_without_a_shell_and_failure_blocks(self):
        checker='/nix/store/'+'a'*32+'-check'
        env={'NATIVE_BUILD_ADMISSION_COMMAND':checker}
        for code in [0,75]:
            with self.subTest(code=code), patch.object(channel.os, 'access', return_value=True), patch.object(channel.subprocess, 'run', return_value=subprocess.CompletedProcess([],code)) as run:
                if code:
                    with self.assertRaises(ValueError):
                        channel.native_build_ready({'forge':'forgejo'}, 'aarch64-darwin', env)
                else:
                    channel.native_build_ready({'forge':'forgejo'}, 'aarch64-darwin', env)
                self.assertEqual(run.call_args.args[0],[checker])
                self.assertNotIn('shell',run.call_args.kwargs)
                self.assertEqual(run.call_args.kwargs['timeout'],15)
