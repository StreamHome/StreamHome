"""Actual paired-link crash/recovery; finite root permission and build proof substituted."""
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import managed_update, runtime_links, state, update_controller, update_link_recovery
from lifecycle import update_recovery
from lifecycle.runtime_build import RuntimeBuildError
from tests.lifecycle import test_update_runtime_links as link_fixture


class UpdateLinkRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = link_fixture.UpdateRuntimeLinkTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation
        self.enterContext(patch.object(update_link_recovery, 'layout_for_root',
            side_effect=lambda root: runtime_links.layout_for_root(root)))
        original = os.replace
        def fail_second(source, destination):
            if destination == fixture.web/'dist':raise OSError('Interrupted original paired-link publication')
            return original(source, destination)
        with patch.object(runtime_links.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):fixture.bind('candidate')
        self.source = fixture.receipt.read_bytes()
        self.grant = {'runtime_link_source_sha256': update_recovery.digest(json.loads(self.source))}
        self.enterContext(patch.object(update_recovery, 'require_controller_permission', return_value=self.grant))
        self.enterContext(patch.object(update_controller, '_require_managed_progress_permission'))
        fixture.lease.release()
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='exclusive', next_phase='recovery_required')
        update_controller.reconcile_operator_journal_recovery_required(self.root,
            operation_id=self.operation, generation=2, current_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        with patch.object(managed_update.os, 'getpid', return_value=fixture.main):
            self.lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=2, expected_phase='recovery_required', owner_capability=capability, next_owner_kind='updater')
        self.addCleanup(self.lease.release)
        update_controller._record_transferred_generation(self.root, operation_id=self.operation,
            previous_generation=2, generation=3, lifecycle_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            target_commit=fixture.candidate.application_commit, previous_commit=fixture.parent.execution.application_commit)
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=3,
            expected_phase='recovery_required', next_phase='exclusive')
        payload = update_controller.read_update_journal(fixture.path)
        payload.update(phase='rolling_back', lifecycle_phase='exclusive', failed_target=payload['target_commit'])
        fixture.path.write_text(json.dumps(payload))
        self.proof = SimpleNamespace(lease=self.lease.lease, paths=self.lease.paths,
            require_current=self.lease.require_current)
        self.receipt = fixture.receipt.parent/f'runtime-link-update-recovery-{self.operation}-3.json'

    def bind(self):
        with patch.object(managed_update.os, 'getpid', return_value=self.fixture.main):
            runtime_links.bind_update_runtime(self.root, proof=self.proof, target='previous',
                assert_children_stopped=lambda: None)

    def test_new_generation_recovers_exact_mixed_pair_without_rewriting_original_applying_receipt(self):
        self.bind()
        runtime_links.verify_links(self.root, self.fixture.parent.execution)
        self.assertEqual(self.fixture.receipt.read_bytes(), self.source)
        retained = json.loads(self.receipt.read_bytes())
        self.assertEqual(retained['source_generation'], 2);self.assertEqual(retained['generation'], 3)
        self.assertEqual(retained['stage'], 'complete')
        self.bind()
        self.assertEqual(json.loads(self.receipt.read_bytes()), retained)

    def test_interrupted_recovery_reuses_its_exact_intent_and_preserves_both_attempts(self):
        original = os.replace
        def fail_second(source, destination):
            if destination == self.fixture.web/'dist':raise OSError('Interrupted recovery paired-link publication')
            return original(source, destination)
        with patch.object(runtime_links.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):self.bind()
        self.assertEqual(json.loads(self.receipt.read_bytes())['stage'], 'applying')
        self.assertEqual(self.fixture.receipt.read_bytes(), self.source)
        self.bind();runtime_links.verify_links(self.root, self.fixture.parent.execution)
        self.assertEqual(json.loads(self.receipt.read_bytes())['stage'], 'complete')

    def test_changed_review_source_foreign_pair_or_other_recovery_intent_is_preserved(self):
        before = self.fixture.links(), self.fixture.receipt.read_bytes()
        self.grant['runtime_link_source_sha256'] = 'f'*64
        with self.assertRaises(RuntimeBuildError):self.bind()
        self.assertFalse(self.receipt.exists());self.assertEqual((self.fixture.links(), self.fixture.receipt.read_bytes()), before)
        self.grant['runtime_link_source_sha256'] = update_recovery.digest(json.loads(self.source))
        self.fixture.web.joinpath('dist').unlink();self.fixture.web.joinpath('dist').symlink_to('/foreign/assets')
        with self.assertRaises(RuntimeBuildError):self.bind()
        self.assertFalse(self.receipt.exists());self.assertEqual(self.fixture.receipt.read_bytes(), self.source)
