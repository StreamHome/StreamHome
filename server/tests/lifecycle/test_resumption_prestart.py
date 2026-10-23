from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.control import provisioning, resumption_dispatch, resumption_store, startup
from lifecycle.control.rpc import ControlError
from lifecycle.installation import InstallationStatus


class ResumptionPrestartTests(unittest.TestCase):
    def invoke(self):return provisioning.before_start(control_manifest_sha256='a' * 64)

    def fixtures(self, stack, required):
        stack.enter_context(patch.object(provisioning, 'require_root'))
        stack.enter_context(patch.object(provisioning, 'descriptor', return_value=SimpleNamespace(status=InstallationStatus.READY)))
        stack.enter_context(patch.object(provisioning.Path, 'read_text', return_value='0::/system.slice/streamhome.service\n'))
        stack.enter_context(patch.object(resumption_store, 'START_INTENT', SimpleNamespace(exists=lambda: True, is_symlink=lambda: False)))
        stack.enter_context(patch.object(provisioning, 'run_worker', return_value={'required': required}))

    def test_exact_resumption_hook_bypasses_only_the_already_held_issuer_lock(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.fixtures(stack, True)
            exclusion = stack.enter_context(patch.object(provisioning, 'administrative_lock', side_effect=AssertionError('issuer owns this lock')))
            bind = stack.enter_context(patch.object(resumption_dispatch, 'before_start'))
            schedule = stack.enter_context(patch.object(startup, 'schedule_pending'))
            self.assertEqual(self.invoke(), 0)
            bind.assert_called_once_with(control_manifest_sha256='a' * 64)
            exclusion.assert_not_called();schedule.assert_not_called()

    def test_invalid_live_resumption_never_falls_back_to_an_ordinary_start(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.fixtures(stack, True)
            stack.enter_context(patch.object(resumption_dispatch, 'before_start', side_effect=ControlError('control_resumption_issuer_changed')))
            exclusion = stack.enter_context(patch.object(provisioning, 'administrative_lock'))
            schedule = stack.enter_context(patch.object(startup, 'schedule_pending'))
            with self.assertRaises(ControlError):self.invoke()
            exclusion.assert_not_called();schedule.assert_not_called()

    def test_stale_os_intent_does_not_override_resolved_canonical_normal_start(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.fixtures(stack, False)
            events = []
            @contextmanager
            def exclusion():
                events.append('locked');yield;events.append('released')
            stack.enter_context(patch.object(provisioning, 'administrative_lock', side_effect=exclusion))
            schedule = stack.enter_context(patch.object(startup, 'schedule_pending', side_effect=lambda: events.append('scheduled')))
            bind = stack.enter_context(patch.object(resumption_dispatch, 'before_start'))
            self.assertEqual(self.invoke(), 0)
            self.assertEqual(events, ['locked', 'released', 'scheduled'])
            bind.assert_not_called();schedule.assert_called_once()


if __name__ == '__main__':unittest.main()
