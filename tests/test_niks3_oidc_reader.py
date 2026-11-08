"""Exercise OIDC cache credentials without real tokens, network, or workers."""
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import netrc
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
NIX_BINARY = shutil.which("nix")


def load_reader():
    spec = importlib.util.spec_from_file_location("oidc_reader_under_test", ROOT / "ci/configure-niks3.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_token(expiry, label="initial", audience="https://niks3.monke.fail/read"):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "fixture"}) + "." + encode({
        "exp": expiry, "aud": audience, "fixture": label,
    }) + ".synthetic-signature"


def response(value):
    return io.StringIO(json.dumps({"value": value}))


class Niks3OIDCReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.epoch = int(datetime.now(timezone.utc).timestamp())
        self.reader = load_reader()
        self.original_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.original_umask)
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "RUNNER_TEMP": str(self.root),
            "GITHUB_ENV": str(self.root / "github-env"),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "fixture/owned-tool",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://fixture.actions.githubusercontent.com/token?api-version=2.0&audience=old",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-request-credential",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.worker = patch.object(self.reader.subprocess, "Popen")
        self.popen = self.worker.start()
        self.addCleanup(self.worker.stop)

    @property
    def directory(self):
        return self.root / "flakey-niks3-auth"

    def configure(self, token=None):
        token = token or fixture_token(self.epoch + 600)
        with patch("urllib.request.urlopen", return_value=response(token)) as request:
            with redirect_stdout(io.StringIO()) as output:
                self.reader.configure(oidc=True)
        return token, request, output.getvalue()

    def test_trusted_initial_reader_uses_oidc_url_and_only_intended_host(self):
        token, request, _ = self.configure()
        config = (self.directory / "nix.conf").read_text()
        self.assertIn("extra-substituters = https://niks3.monke.fail/oidc\n", config)
        self.assertIn("extra-trusted-public-keys = " + self.reader.KEY, config)
        self.assertIn("netrc-file = " + str(self.directory / "netrc"), config)
        credentials = netrc.netrc(str(self.directory / "netrc"))
        self.assertEqual(set(credentials.hosts), {"niks3.monke.fail"})
        self.assertEqual(credentials.authenticators("niks3.monke.fail"), ("oidc", "", token))
        sent = request.call_args.args[0]
        self.assertEqual(urlsplit(sent.full_url).hostname, "fixture.actions.githubusercontent.com")
        self.assertEqual(parse_qs(urlsplit(sent.full_url).query)["audience"], ["https://niks3.monke.fail/read"])
        self.assertEqual(sent.get_header("Authorization"), "Bearer synthetic-request-credential")

    def test_owning_repository_dispatch_can_acquire_a_read_only_token(self):
        os.environ['GITHUB_EVENT_NAME'] = 'repository_dispatch'
        _, request, _ = self.configure()
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0].full_url).query)['audience'],
                         ['https://niks3.monke.fail/read'])

    def test_token_source_keeps_publisher_and_reader_audiences_separate(self):
        spec = importlib.util.spec_from_file_location("oidc_token_source_under_test", ROOT / "ci/niks3-oidc-token.py")
        source = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source)
        for audience, kwargs in (
            ("https://niks3.monke.fail", {}),
            ("https://niks3.monke.fail/read", {"audience": "https://niks3.monke.fail/read"}),
        ):
            with self.subTest(audience=audience):
                token = fixture_token(self.epoch + 600, audience=audience)
                with patch.object(source, "urlopen", return_value=response(token)) as request:
                    acquired = source.token(**kwargs)
                self.assertEqual(acquired["token"], token)
                sent = request.call_args.args[0]
                self.assertEqual(parse_qs(urlsplit(sent.full_url).query)["audience"], [audience])
        self.popen.assert_not_called()

    def test_credentials_and_expiry_are_private_and_not_exported(self):
        token, _, output = self.configure()
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for name in ("netrc", "nix.conf", "expires-at"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600, name)
        self.assertAlmostEqual(float((self.directory / "expires-at").read_text()), self.epoch + 600, delta=1)
        exported = Path(os.environ["GITHUB_ENV"]).read_text()
        self.assertIn("NIKS3_READ_AUTH=github-oidc\n", exported)
        self.assertIn("NIKS3_READ_URL=https://niks3.monke.fail/oidc\n", exported)
        self.assertNotIn(token, exported + output)
        self.assertNotIn("synthetic-request-credential", exported + output)
        self.assertNotIn("password ", exported)
        self.assertEqual(self.popen.call_count, 1)
        self.assertIn("--refresh-oidc", self.popen.call_args.args[0])
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])

    def test_existing_user_configuration_remains_in_configuration_search_path(self):
        os.environ["NIX_USER_CONF_FILES"] = "/fixture/first.conf:/fixture/second.conf"
        self.configure()
        exported = Path(os.environ["GITHUB_ENV"]).read_text()
        self.assertIn("NIX_USER_CONF_FILES=" + str(self.directory / "nix.conf") + ":/fixture/first.conf:/fixture/second.conf", exported)

    def test_existing_config_cannot_shadow_the_job_reader(self):
        nix = NIX_BINARY
        if nix is None:
            self.skipTest("Nix is needed to exercise its actual configuration precedence")
        previous = self.root / "previous.conf"
        previous.write_text("netrc-file = " + str(self.root / "managed.netrc") + "\n")
        os.environ["NIX_USER_CONF_FILES"] = str(previous)
        self.configure()
        exported = dict(line.split("=", 1) for line in Path(os.environ["GITHUB_ENV"]).read_text().splitlines())
        environment = dict(os.environ, NIX_CONF_DIR=str(self.root / "etc"), NIX_CONFIG="",
                           NIX_USER_CONF_FILES=exported["NIX_USER_CONF_FILES"])
        # The refresher was mocked during configure; run the real Nix process.
        self.worker.stop()
        result = subprocess.run([nix, "config", "show", "--json", "--extra-experimental-features", "nix-command"],
                                env=environment, capture_output=True, text=True, check=True, timeout=30)
        settings = json.loads(result.stdout)
        self.assertEqual(settings["netrc-file"]["value"], str(self.directory / "netrc"))
        self.assertIn("https://niks3.monke.fail/oidc", settings["substituters"]["value"])

    def test_github_pull_request_events_fail_before_network_or_worker(self):
        for event in ("pull_request", "pull_request_target", "workflow_run", "issue_comment"):
            with self.subTest(event=event), patch.dict(os.environ, {"GITHUB_EVENT_NAME": event}):
                with patch("urllib.request.urlopen") as request:
                    with self.assertRaises(ValueError):
                        self.reader.configure(oidc=True)
                request.assert_not_called()
                self.popen.assert_not_called()
                self.assertFalse((self.directory / "netrc").exists())

    def test_other_github_servers_and_token_service_origins_fail_closed(self):
        cases = [
            {"GITHUB_SERVER_URL": "https://github.example"},
            {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.githubusercontent.com.attacker.example/token"},
            {"ACTIONS_ID_TOKEN_REQUEST_URL": "http://fixture.actions.githubusercontent.com/token"},
            {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://user:password@fixture.actions.githubusercontent.com/token"},
        ]
        for values in cases:
            with self.subTest(values=values), patch.dict(os.environ, values):
                with patch("urllib.request.urlopen") as request:
                    with self.assertRaises(ValueError):
                        self.reader.configure(oidc=True)
                request.assert_not_called()
                self.popen.assert_not_called()

    def test_static_fleet_credentials_cannot_be_mixed_with_github_oidc(self):
        for key in ("NIKS3_READ_NETRC", "NIKS3_WRITE_TOKEN", "NIKS3_AUTH_TOKEN_FILE"):
            with self.subTest(key=key), patch.dict(os.environ, {key: "synthetic-static-credential"}):
                with patch("urllib.request.urlopen") as request:
                    with self.assertRaises(ValueError):
                        self.reader.configure(oidc=True)
                request.assert_not_called()
                self.popen.assert_not_called()

    def test_expired_or_nearly_expired_tokens_are_not_installed(self):
        for expiry in (self.epoch - 60, self.epoch + 10):
            with self.subTest(expiry=expiry):
                with patch("urllib.request.urlopen", return_value=response(fixture_token(expiry))):
                    with self.assertRaises(ValueError):
                        self.reader.configure(oidc=True)
                self.assertFalse((self.directory / "netrc").exists())
                self.popen.assert_not_called()

    def test_malformed_token_response_does_not_create_reader(self):
        for value in (None, "not-a-jwt", "header.bad payload.signature", "h.e30.s"):
            with self.subTest(value=value):
                with patch("urllib.request.urlopen", return_value=response(value)):
                    with self.assertRaises((ValueError, KeyError, TypeError)):
                        self.reader.configure(oidc=True)
                self.assertFalse((self.directory / "netrc").exists())
                self.popen.assert_not_called()

    def test_static_reader_rejects_credentials_for_another_host(self):
        os.environ["NIKS3_READ_NETRC"] = "machine unrelated.example login fixture password synthetic"
        with self.assertRaises(ValueError):
            self.reader.configure()
        self.popen.assert_not_called()
        self.assertFalse((self.root / "github-env").exists())

    def test_refresh_replaces_credentials_before_initial_token_expires(self):
        initial, _, _ = self.configure(fixture_token(self.epoch + 60))
        refreshed = fixture_token(self.epoch + 600, "refreshed")
        elapsed = [0.0]
        acquisition_times = []
        replaced = []

        def acquire(request, **kwargs):
            acquisition_times.append(elapsed[0])
            self.assertLess(elapsed[0], 60, "reader refreshed only after its token expired")
            self.assertIn(initial, (self.directory / "netrc").read_text())
            return response(refreshed)

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 10, "cleanup must be observed promptly")
            elapsed[0] += seconds
            if acquisition_times:
                replaced.append((self.directory / "netrc").read_text())
                self.assertEqual(stat.S_IMODE((self.directory / "netrc").stat().st_mode), 0o600)
                self.reader.cleanup()
            self.assertLess(elapsed[0], 100, "refresh loop did not stop after cleanup")

        with patch("urllib.request.urlopen", side_effect=acquire), patch.object(self.reader.time, "time", side_effect=lambda: self.epoch + elapsed[0]), patch.object(self.reader.time, "sleep", side_effect=sleep):
            self.reader.refresh_oidc()
        self.assertEqual(len(acquisition_times), 1)
        self.assertTrue(replaced)
        self.assertIn(refreshed, replaced[0])
        self.assertNotIn(initial, replaced[0])
        self.assertFalse(self.directory.exists())

    def test_transient_refresh_failure_keeps_valid_reader_then_retries(self):
        initial, _, _ = self.configure(fixture_token(self.epoch + 90))
        refreshed = fixture_token(self.epoch + 600, "after-retry")
        elapsed = [0.0]
        attempts = []

        def acquire(request, **kwargs):
            attempts.append(elapsed[0])
            self.assertIn(initial, (self.directory / "netrc").read_text())
            if len(attempts) == 1:
                raise URLError("synthetic temporary outage")
            return response(refreshed)

        def sleep(seconds):
            elapsed[0] += seconds
            if len(attempts) >= 2:
                self.assertIn(refreshed, (self.directory / "netrc").read_text())
                self.reader.cleanup()
            self.assertLess(elapsed[0], 90)

        with patch("urllib.request.urlopen", side_effect=acquire), patch.object(self.reader.time, "time", side_effect=lambda: self.epoch + elapsed[0]), patch.object(self.reader.time, "sleep", side_effect=sleep):
            self.reader.refresh_oidc()
        self.assertEqual(len(attempts), 2)
        self.assertLessEqual(attempts[1] - attempts[0], 5)

    def test_cleanup_stops_waiting_refresh_without_acquiring_another_token(self):
        self.configure()

        def sleep(seconds):
            self.assertLessEqual(seconds, 10)
            self.reader.cleanup()

        with patch("urllib.request.urlopen") as request, patch.object(self.reader.time, "sleep", side_effect=sleep):
            self.reader.refresh_oidc()
        request.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_refresh_outage_removes_expired_reader_instead_of_reusing_it(self):
        self.configure(fixture_token(self.epoch + 60))
        elapsed = [0.0]

        def sleep(seconds):
            elapsed[0] += seconds
            self.assertLess(elapsed[0], 90, "expired reader retries forever")

        with patch("urllib.request.urlopen", side_effect=URLError("synthetic outage")), patch.object(self.reader.time, "time", side_effect=lambda: self.epoch + elapsed[0]), patch.object(self.reader.time, "sleep", side_effect=sleep):
            with self.assertRaises(ValueError):
                self.reader.refresh_oidc()
        self.assertFalse((self.directory / "netrc").exists())


if __name__ == "__main__":
    unittest.main()
