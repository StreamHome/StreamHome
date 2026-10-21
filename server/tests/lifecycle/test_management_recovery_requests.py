from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import recover as command, recovery_requests as grants
from lifecycle.control.requests import parse_management_request
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_management_requests import request_payload


class RecoveryGrantTests(unittest.TestCase):
    def setUp(self):
        self.original = parse_management_request(request_payload())
        self.current = replace(self.original, expected_generation=self.original.expected_generation + 1,
            supervisor=replace(self.original.supervisor, pid=127), backend=None,
            previous_boot_enabled=True, boot_enabled=True, stop_epoch='d' * 64)
        self.grant = {'schema_version': 1, 'recovery_review_id': str(uuid.uuid4()), 'source_evidence_sha256': 'e' * 64,
            'source_request_sha256': self.original.sha256, 'action': 'continue_recovery', 'request': self.current.payload(),
            'expires_at_monotonic': 1000.0}

    def test_recovery_preserves_reviewed_tuple_settings_and_original_intent(self):
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            self.assertEqual(grants.parse_grant(self.grant), self.grant)
            for key, value in (('candidate', self.original.payload()['previous']), ('deployment', {**self.current.deployment.payload(), 'web_port': 4444}),
                               ('supervisor_invocation_id', '1' * 32), ('initial_service_state', 'stopped')):
                changed = deepcopy(self.grant)
                changed['request'][key] = value
                with self.subTest(key=key), self.assertRaises((ValueError, ControlError)):
                    grants.parse_grant(changed)

    def test_expiry_stop_and_wrong_generation_block_even_valid_root_grants(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(grants.request_contract, 'read_management_request', return_value=self.original))
            stack.enter_context(patch.object(grants, 'read_protected_json', return_value=self.grant))
            stack.enter_context(patch.object(grants.Path, 'read_text', return_value=self.current.boot_id))
            stack.enter_context(patch.object(grants, 'read_stop_epoch', return_value=self.current.stop_epoch))
            clock = stack.enter_context(patch.object(grants.time, 'monotonic', return_value=100))
            self.assertEqual(grants.read_grant(self.current.operation_id, self.current.expected_generation + 1), self.grant)
            clock.return_value = 1001
            with self.assertRaisesRegex(ControlError, 'control_recovery_permission_expired'):
                grants.read_grant(self.current.operation_id, self.current.expected_generation + 1)
            self.assertEqual(grants.read_grant(self.current.operation_id, self.current.expected_generation + 1, require_live=False), self.grant)
            clock.return_value = 100
            with self.assertRaises(ControlError):
                grants.read_grant(self.current.operation_id, self.current.expected_generation + 2)
            self.grant['request']['stop_epoch'] = 'f' * 64
            with self.assertRaisesRegex(ControlError, 'control_recovery_permission_expired'):
                grants.read_grant(self.current.operation_id, self.current.expected_generation + 1)

    def test_unknown_nonfinite_and_boolean_expiry_are_not_permissions(self):
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            for extra in ({'expires_at_monotonic': True}, {'expires_at_monotonic': float('nan')}, {'command': 'arbitrary'}, {'action': 'rollback'}):
                with self.subTest(extra=extra), self.assertRaises(ControlError):
                    grants.parse_grant({**self.grant, **extra})

    def test_noninteractive_decline_and_stale_review_never_imply_consent(self):
        snapshot = {'operation_id': self.current.operation_id, 'generation': 8, 'evidence_sha256': 'e' * 64}
        value = {'schema_version': 1, 'operation_id': self.current.operation_id, 'expected_generation': 8,
            'expected_evidence_sha256': 'e' * 64, 'continue_recovery': True, 'interruption_approved': True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.json'
            path.write_text(json.dumps(value))
            self.assertTrue(command._consent(path, snapshot))
            path.write_text(json.dumps({**value, 'interruption_approved': False}))
            self.assertFalse(command._consent(path, snapshot))
            path.write_text(json.dumps({**value, 'expected_generation': 7}))
            with self.assertRaisesRegex(ControlError, 'control_recovery_review_stale'):
                command._consent(path, snapshot)

    def test_root_recovery_command_does_not_import_canonical_or_application_code(self):
        script = "import sys; import lifecycle.control.recover; assert not {'lifecycle.state','lifecycle.control.recovery_requests','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
