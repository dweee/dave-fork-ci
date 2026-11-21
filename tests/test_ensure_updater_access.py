"""Updater access helper: idempotent grants, clear non-secret failures, no network."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ensure_access', ROOT / 'ci/ensure-updater-access.py')
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)

TOKEN = 'fixture-admin-token-value'
ENV = {'FORGEJO_ADMIN_TOKEN': TOKEN}


def run(argv, responses, environ=ENV):
    """Run main() with scripted API results; returns (code, stdout, stderr, calls)."""
    calls = []
    def fake(host, token, method, path, data=None):
        calls.append((method, path, data))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    out, err = io.StringIO(), io.StringIO()
    with patch.object(e, 'request', side_effect=fake), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = e.main(argv, environ)
    return code, out.getvalue(), err.getvalue(), calls


def http(code):
    return HTTPError('https://' + TOKEN + '@git.monke.fail/x', code, TOKEN, None, None)


class EnsureAccess(unittest.TestCase):
    def test_missing_collaborator_is_granted_write(self):
        code, out, err, calls = run(['--repository', 'd/vacwatch'], [{'permission': 'none'}, None])
        self.assertEqual(code, 0)
        self.assertIn('d/vacwatch: flakey-updater write access granted', out)
        self.assertEqual(calls[1], ('PUT', '/repos/d/vacwatch/collaborators/flakey-updater', {'permission': 'write'}))

    def test_not_found_permission_counts_as_none(self):
        code, out, _, calls = run(['--repository', 'd/vacwatch'], [http(404), None])
        self.assertEqual((code, calls[1][0]), (0, 'PUT'))

    def test_existing_write_or_admin_is_left_alone(self):
        for have in ['write', 'admin']:
            code, out, _, calls = run(['--repository', 'd/vacwatch'], [{'permission': have}])
            self.assertEqual(code, 0)
            self.assertIn('access ok', out)
            self.assertEqual(len(calls), 1)

    def test_read_access_is_raised_never_lowered(self):
        code, out, _, calls = run(['--repository', 'd/vacwatch'], [{'permission': 'read'}, None])
        self.assertIn('access raised', out)
        self.assertEqual(calls[1][2], {'permission': 'write'})
        code, out, _, calls = run(['--repository', 'd/vacwatch', '--permission', 'read'], [{'permission': 'write'}])
        self.assertIn('access ok', out)
        self.assertEqual(len(calls), 1)

    def test_check_mode_reports_missing_without_writing(self):
        code, out, _, calls = run(['--check', '--repository', 'd/a', '--repository', 'd/b'],
                                  [{'permission': 'none'}, {'permission': 'write'}])
        self.assertEqual(code, 1)
        self.assertIn('d/a: flakey-updater write access missing', out)
        self.assertIn('d/b: flakey-updater write access ok', out)
        self.assertEqual({c[0] for c in calls}, {'GET'})

    def test_failure_is_clear_continues_and_never_prints_the_token(self):
        code, out, err, calls = run(['--repository', 'd/a', '--repository', 'd/b'],
                                    [http(403), {'permission': 'write'}])
        self.assertEqual(code, 1)
        self.assertIn('d/a: HTTP 403, the token needs repository admin rights', err)
        self.assertIn('d/b: flakey-updater write access ok', out)
        self.assertNotIn(TOKEN, out + err)

    def test_missing_token_and_bad_input_fail_before_any_request(self):
        code, _, err, calls = run(['--repository', 'd/a'], [], environ={})
        self.assertEqual((code, calls), (2, []))
        self.assertIn('FORGEJO_ADMIN_TOKEN', err)
        code, _, err, calls = run(['--repository', '../x/y/z'], [])
        self.assertEqual((code, calls), (1, []))
        code, _, err, calls = run(['--repository', 'd/a', '--host', 'http://git.monke.fail'], [])
        self.assertEqual((code, calls), (2, []))


class WorkflowTimeHint(unittest.TestCase):
    def test_404_in_a_forge_phase_points_at_the_access_helper(self):
        fspec = importlib.util.spec_from_file_location('forge', ROOT / 'ci/automation-forge.py')
        forge = importlib.util.module_from_spec(fspec)
        fspec.loader.exec_module(forge)
        forge.ACTIVE_PHASE = 'find-existing-input-pr'
        text = forge.failure_detail(HTTPError('https://u:p@x', 404, 'm', None, None))
        self.assertIn('missing-input-or-output (HTTP 404)', text)
        self.assertIn('ensure-updater-access.py', text)
        self.assertNotIn('u:p', text)
        forge.ACTIVE_PHASE = 'push-input-branch'
        self.assertNotIn('ensure-updater', forge.failure_detail(HTTPError('u', 404, 'm', None, None)))
        forge.ACTIVE_PHASE = 'create-input-pr'
        self.assertNotIn('ensure-updater', forge.failure_detail(HTTPError('u', 403, 'm', None, None)))


if __name__ == '__main__':
    unittest.main()
