from contextlib import ExitStack
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

from lifecycle.control import listener_review as review
from lifecycle.control.rpc import ControlError
from lifecycle.deployment_config import default_deployment, DeploymentConfigError


class ListenerReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.config = default_deployment()
        self.worker = self.stack.enter_context(patch.object(review, 'run_worker'))
        self.epoch = self.stack.enter_context(patch.object(review, 'read_stop_epoch', return_value='a' * 64))
        self.clock = self.stack.enter_context(patch.object(review.time, 'monotonic', return_value=0.0))
        self.pause = self.stack.enter_context(patch.object(review.time, 'sleep'))
        self.output = self.stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
        self.ready = {'available': True, 'owned': False, 'bind': self.config.web_bind, 'port': self.config.web_port}
        self.closing = {**self.ready, 'available': False, 'reason': 'closing_connections'}

    def check(self):
        return review.check_requested_listener(self.config, web=None, invocation=None)

    def test_closing_connections_get_bounded_visible_retries_then_exact_free_probe(self):
        self.worker.side_effect = [self.closing, self.closing, self.ready]
        self.assertIn('Listener preflight passed', self.check()[0])
        self.assertEqual(self.pause.call_count, 2)
        self.assertEqual(self.output.getvalue().count('Waiting up to 65 seconds'), 1)
        self.assertTrue(all(call.args == (['check-listener'],) for call in self.worker.call_args_list))

    def test_foreign_conflict_after_closing_expiry_never_becomes_available(self):
        self.worker.side_effect = [self.closing, {**self.closing, 'reason': 'listener_conflict'}]
        with self.assertRaises(review.PortPreflightError) as raised:
            self.check()
        self.assertEqual(raised.exception.code, 'control_web_port_conflict')
        self.assertEqual(self.pause.call_count, 1)

    def test_persistent_closing_reservation_expires_instead_of_waiting_forever(self):
        self.worker.return_value = self.closing
        self.clock.side_effect = [0, 0, 0, 66]
        with self.assertRaises(review.PortPreflightError) as raised:
            self.check()
        self.assertEqual(raised.exception.code, 'control_web_port_closing')
        self.assertEqual(self.worker.call_count, 2)

    def test_direct_stop_during_wait_cancels_without_another_listener_probe(self):
        self.worker.return_value = self.closing
        self.pause.side_effect = lambda _: setattr(self.epoch, 'return_value', 'b' * 64)
        with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
            self.check()
        self.assertEqual(self.worker.call_count, 1)

    def test_changed_endpoint_or_nonboolean_result_is_not_success(self):
        for changed in ({'available': 1}, {'port': 4000}, {'bind': '0.0.0.0'}):
            self.worker.return_value = {**self.ready, **changed}
            with self.subTest(changed=changed), self.assertRaises(DeploymentConfigError):
                self.check()
        self.pause.assert_not_called()

    def test_root_preflight_module_does_not_import_canonical_or_application_code(self):
        code = "import sys; import lifecycle.control.listener_review; assert not {'lifecycle.state','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
