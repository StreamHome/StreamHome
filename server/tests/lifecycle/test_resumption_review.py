from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import resumption_review as review
from lifecycle.control.rpc import ControlError


class FreshResumptionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'request.json'
        self.boot = str(uuid.uuid4())
        self.snapshot = {'installation_id': str(uuid.uuid4()), 'operation_id': str(uuid.uuid4()),
            'generation': 4, 'initial_service_state': 'running', 'activation_mode': 'cutover',
            'management_action': 'configure_apply', 'permitted_actions': ['continue_recovery', 'rollback'],
            'control_manifest_sha256': 'b' * 64, 'evidence_sha256': 'c' * 64,
            'previous_execution_sha256': 'd' * 64, 'selected_execution_sha256': 'e' * 64,
            'previous_boot_enabled': False}
        self.evidence = {'owner': {'pid': 123, 'nonce': 'f' * 64}, 'scope': {'unit': 'test-only'}}
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(review, 'require_root'))
        stack.enter_context(patch.object(review, 'require_stopped'))
        stack.enter_context(patch.object(review, 'root_stop_epoch', return_value='a' * 64))
        stack.enter_context(patch.object(review, '_boot_enabled', return_value=True))
        stack.enter_context(patch.object(review.Path, 'read_text', return_value=self.boot))
        stack.enter_context(patch.object(review.time, 'monotonic', return_value=100))
        self.issuer = stack.enter_context(patch.object(review, 'current_issuer', return_value=self.evidence))
        self.current = self.make()

    def make(self, action='continue_recovery'):
        return review.make_review(self.snapshot, action, control_manifest_sha256=self.snapshot['control_manifest_sha256'])

    def request(self, current=None, **changed):
        current = self.current if current is None else current
        value = {'schema_version': 3, 'operation_id': self.snapshot['operation_id'],
            'expected_generation': self.snapshot['generation'], 'expected_evidence_sha256': self.snapshot['evidence_sha256'],
            'action': current.payload['recovery_action'], 'continue_recovery': True, 'interruption_approved': True,
            'resume_service': True, 'expected_resumption_sha256': current.sha256}
        value.update(changed)
        self.path.write_text(json.dumps(value))

    def consent(self, current=None):
        with redirect_stdout(io.StringIO()):return review.consent(self.current if current is None else current, self.path)

    def test_fresh_scoped_consent_can_create_only_the_reviewed_generation_and_tuple(self):
        self.request();approved = self.consent()
        plan = review.launch_plan(self.current, approved=approved)
        self.assertEqual(plan['generation'], self.snapshot['generation'] + 1)
        self.assertEqual(plan['plan_id'], self.current.payload['plan_id'])
        self.assertEqual(plan['review_id'], self.current.payload['review_id'])
        self.assertEqual(plan['stages'][0]['execution_sha256'], self.snapshot['selected_execution_sha256'])
        self.assertEqual(len(plan['stages']), 1)
        self.assertTrue(self.current.payload['boot_enabled'])

    def test_old_file_cannot_approve_a_new_issuer_review_of_unchanged_canonical_state(self):
        self.request()
        with self.assertRaises(ControlError):self.consent(self.make())
        self.assertEqual(self.current.source, self.snapshot)

    def test_legacy_forward_consent_or_implicit_truthy_values_cannot_authorize_a_start(self):
        for changed in ({'schema_version': 1}, {'resume_service': 1}, {'expected_generation': True},
                        {'expected_resumption_sha256': 'f' * 64}, {'expected_evidence_sha256': 'e' * 64}):
            with self.subTest(changed=changed):
                self.request(**changed)
                with self.assertRaises(ControlError):self.consent()

    def test_decline_does_not_mint_a_permission(self):
        self.request(resume_service=False)
        approved = self.consent();self.assertFalse(approved)
        with self.assertRaises(ControlError):review.launch_plan(self.current, approved=approved)

    def test_stop_or_changed_issuer_after_consent_prevents_plan_creation(self):
        self.request();approved = self.consent()
        with patch.object(review, 'root_stop_epoch', return_value='f' * 64):
            with self.assertRaises(ControlError):review.launch_plan(self.current, approved=approved)
        self.issuer.return_value = {'owner': {'pid': 124}, 'scope': {'unit': 'changed'}}
        with self.assertRaises(ControlError):review.launch_plan(self.current, approved=approved)

    def test_previous_runtime_review_keeps_original_boot_target_and_exact_previous_tuple(self):
        previous = self.make('rollback')
        self.assertEqual(previous.payload['execution_sha256'], self.snapshot['previous_execution_sha256'])
        self.assertFalse(previous.payload['boot_enabled'])
        self.assertTrue(previous.payload['previous_boot_enabled'])

    def test_waiting_for_a_fresh_response_has_a_finite_deadline(self):
        with patch.object(review.time, 'monotonic', side_effect=[100, 1001]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ControlError, 'control_resumption_review_timeout'):
                review.consent(self.current, self.path)

    def test_root_review_imports_no_canonical_or_application_code(self):
        script = "import sys;import lifecycle.control.resumption_review;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':unittest.main()
