"""New callers must grant OIDC without introducing static cache credentials."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('render_caller', ROOT / 'bin/render_caller.py')
caller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caller)


class CallerAuthentication(unittest.TestCase):
    def test_generated_caller_grants_oidc_and_pins_both_controller_references(self):
        revision = 'a' * 40
        text = caller.render('example/controller', revision)
        self.assertIn('      id-token: write\n', text)
        self.assertIn('example/controller/.github/workflows/upstream-sync.yml@' + revision, text)
        self.assertIn('tool-revision: ' + revision, text)
        self.assertIn('repository_dispatch:', text)
        self.assertNotIn('secrets:', text)
        self.assertNotIn('ATTIC_', text)
        self.assertNotIn('attic-', text)

    def test_mutable_controller_references_are_rejected(self):
        for repository, revision in [('example/controller', 'main'), ('controller', 'a' * 40)]:
            with self.subTest(repository=repository, revision=revision), self.assertRaises(ValueError):
                caller.render(repository, revision)
