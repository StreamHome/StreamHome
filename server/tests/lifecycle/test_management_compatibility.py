"""Run the actual restore/ASGI/crash/acceptance regressions on authority v2.

Use tests/run_isolated.py with the existing dependency environment. These tests
initialize disposable v2 fixtures; they do not migrate an existing installation.
"""
from lifecycle import state
from tests.lifecycle import test_guarded_restore_asgi as guarded
from tests.lifecycle import test_update_controller as updater


class AuthorityVersionTwoRestoreTests(guarded.GuardedRestoreASGIRegression):
    def setUp(self):
        self.installation = guarded.DisposableRestoreInstallation(authority_schema_version=2)

    def tearDown(self):
        try:
            self.assertEqual(state.inspect_state(self.installation.root).schema_version, 2)
        finally:
            self.installation.close()


class AuthorityVersionTwoUpdateTests(updater.UpdateControllerTests):
    authority_schema_version = 2

    def tearDown(self):
        try:
            self.assertEqual(state.inspect_state(self.root).schema_version, 2)
        finally:
            super().tearDown()
