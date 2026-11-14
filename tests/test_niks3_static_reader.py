"""Exercise static Niks3 readers, the independent writer, cleanup and the CLI.

The OIDC reader has its own tests. These cover the credential paths that fleet
runners and publishers use with configured secrets, all with synthetic values.
"""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci/configure-niks3.py"
READER = "machine niks3.monke.fail login fixture password synthetic-read-secret"
WRITER = "W" * 48


def load_reader():
    spec = importlib.util.spec_from_file_location("static_reader_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Niks3StaticReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = load_reader()
        self.original_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.original_umask)
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "RUNNER_TEMP": str(self.root),
            "GITHUB_ENV": str(self.root / "github-env"),
            "NIKS3_READ_NETRC": READER,
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.worker = patch.object(self.reader.subprocess, "Popen")
        self.popen = self.worker.start()
        self.addCleanup(self.worker.stop)

    @property
    def directory(self):
        return self.root / "flakey-niks3-auth"

    def exported(self):
        path = self.root / "github-env"
        if not path.exists():
            return {}
        return dict(line.split("=", 1) for line in path.read_text().splitlines())

    def configure(self):
        with redirect_stdout(io.StringIO()) as output:
            self.reader.configure()
        return output.getvalue()

    def assert_rejected(self):
        with self.assertRaises(ValueError):
            self.configure()
        self.popen.assert_not_called()
        self.assertEqual(self.exported(), {})

    def test_static_reader_uses_the_plain_cache_url_and_private_files(self):
        output = self.configure()
        self.assertNotIn("synthetic-read-secret", output)
        netrc_file = self.directory / "netrc"
        self.assertEqual(netrc_file.read_text(), READER + "\n")
        self.assertEqual(stat.S_IMODE(netrc_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        config = (self.directory / "nix.conf").read_text()
        self.assertIn("extra-substituters = https://niks3.monke.fail\n", config)
        self.assertIn("netrc-file = " + str(netrc_file) + "\n", config)
        self.assertIn("extra-trusted-public-keys = " + self.reader.KEY + "\n", config)
        self.assertIn("http2 = false\n", config)
        self.assertNotIn("synthetic-read-secret", config)
        values = self.exported()
        self.assertEqual(values["NIKS3_READ_URL"], "https://niks3.monke.fail")
        self.assertEqual(values["NIKS3_NETRC_FILE"], str(netrc_file))
        self.assertNotIn("NIKS3_READ_AUTH", values)
        self.assertNotIn("NIKS3_AUTH_TOKEN_FILE", values)
        self.assertFalse((self.directory / "expires-at").exists())
        self.assertNotIn("synthetic-read-secret", (self.root / "github-env").read_text())
        # Only the OIDC reader needs a refresh worker.
        self.popen.assert_not_called()

    def test_job_credentials_directory_overrides_runner_temp(self):
        target = self.root / "job-credentials"
        target.mkdir()
        os.environ["NIX_JOB_CREDENTIALS"] = str(target)
        self.configure()
        self.assertTrue((target / "flakey-niks3-auth" / "netrc").exists())
        self.assertFalse(self.directory.exists())

    def test_missing_or_oversized_reader_is_rejected(self):
        for value in ("", "machine niks3.monke.fail login a password " + "b" * 16384):
            with self.subTest(length=len(value)):
                os.environ["NIKS3_READ_NETRC"] = value
                self.assert_rejected()
        del os.environ["NIKS3_READ_NETRC"]
        self.assert_rejected()

    def test_reader_must_name_only_the_cache_with_a_login_and_password(self):
        for value in (
            READER + "\nmachine other.example login fixture password synthetic",
            "default login fixture password synthetic",
            "machine niks3.monke.fail password synthetic",
            "machine niks3.monke.fail login fixture",
            READER + "\nmacdef init\necho synthetic\n",
        ):
            with self.subTest(value=value.splitlines()[-1]):
                os.environ["NIKS3_READ_NETRC"] = value
                with self.assertRaises((ValueError, self.reader.netrc.NetrcParseError)):
                    self.configure()
                self.popen.assert_not_called()
                self.assertEqual(self.exported(), {})

    def test_symlinked_authentication_directory_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.directory.symlink_to(elsewhere)
        self.assert_rejected()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_independent_writer_is_private_and_exported_by_path(self):
        os.environ["NIKS3_WRITE_TOKEN"] = WRITER
        self.configure()
        token = self.directory / "write-token"
        self.assertEqual(token.read_text(), WRITER)
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertEqual(self.exported()["NIKS3_AUTH_TOKEN_FILE"], str(token))
        self.assertNotIn(WRITER, (self.root / "github-env").read_text())

    def test_malformed_writer_is_rejected_without_writing_it(self):
        for value in ("W" * 47, "W" * 129, "W" * 47 + "!", "W" * 47 + " "):
            with self.subTest(length=len(value)):
                os.environ["NIKS3_WRITE_TOKEN"] = value
                self.assert_rejected()
                self.assertFalse((self.directory / "write-token").exists())

    def test_exported_paths_cannot_inject_environment_lines(self):
        for separator in ("\n", "\r"):
            with self.subTest(separator=repr(separator)):
                injected = self.root / ("runner" + separator + "NIX_CONFIG=injected")
                injected.mkdir()
                os.environ["RUNNER_TEMP"] = str(injected)
                with self.assertRaises(ValueError):
                    self.configure()
                # The append opens GITHUB_ENV first; no line may reach it.
                self.assertEqual(self.exported(), {})

    def test_without_github_env_nothing_is_exported(self):
        del os.environ["GITHUB_ENV"]
        self.configure()
        self.assertTrue((self.directory / "netrc").exists())
        self.assertFalse((self.root / "github-env").exists())

    def test_cleanup_removes_credentials_and_refuses_a_symlink(self):
        os.environ["NIKS3_WRITE_TOKEN"] = WRITER
        self.configure()
        self.reader.cleanup()
        self.assertFalse(self.directory.exists())
        self.reader.cleanup()  # Idempotent for `if: always()` steps.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep").write_text("fixture")
        self.directory.symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            self.reader.cleanup()
        self.assertTrue((elsewhere / "keep").exists())


class Niks3ConfigureCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_command(self, *args, **environment):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "RUNNER_TEMP": str(self.root),
               "GITHUB_ENV": str(self.root / "github-env"), **environment}
        result = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)
        return result, (self.root / "flakey-niks3-auth").exists()

    def test_configure_and_cleanup_commands(self):
        result, exists = self.run_command(NIKS3_READ_NETRC=READER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(exists)
        self.assertNotIn("synthetic-read-secret", result.stdout + result.stderr)
        result, exists = self.run_command("--cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(exists)

    def test_failure_exits_nonzero_without_leaking_or_a_traceback(self):
        secret = "machine other.example login fixture password synthetic-leak-check"
        result, _ = self.run_command(NIKS3_READ_NETRC=secret, NIKS3_WRITE_TOKEN="short")
        self.assertEqual(result.returncode, 1)
        self.assertIn("credentials withheld", result.stderr)
        self.assertNotIn("synthetic-leak-check", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
