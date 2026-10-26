"""Actual canonical/journal gap normalization, with protected metadata/OS scope substituted."""
from contextlib import contextmanager
import fcntl
import os
import unittest
from unittest.mock import patch
import uuid

from lifecycle import managed_update, recovery, state, update_controller, update_recovery
from lifecycle import update_recovery_observation as observation
from lifecycle.control import update_grants
from lifecycle.control.rpc import ControlError
from lifecycle.update_recovery import review as canonical_review
from tests.lifecycle import test_update_recovery_observation as gap_fixture


class UpdateGapNormalizationTests(unittest.TestCase):
    def setUp(self):
        fixture = gap_fixture.UpdateTransferGapTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation;self.canonical = fixture.canonical
        fixture.transfer();fixture.grant['expires_at_monotonic'] = .001
        self.enterContext(patch.object(recovery, '_runtime_status', return_value='active'))
        self.view = canonical_review(self.root, self.operation,
            control_manifest_sha256=fixture.fixture.fixture.digest)
        payload = {**fixture.fixture.payload, 'review_id': str(uuid.uuid4()),
            'expected_evidence_sha256': self.view['evidence_sha256']}
        with patch.object(update_recovery, 'review', return_value=self.view):
            self.grant = update_grants.prepare(self.root, payload,
                control_manifest_sha256=fixture.fixture.fixture.digest)
        fixture.reader.side_effect = lambda operation, generation, **kwargs: {
            3: fixture.grant, 4: self.grant}[generation]
        paths = recovery._new_action_paths(self.root, self.operation, 3, action_id=self.grant['review_id'])
        recovery._write_action_record(paths.record_path, recovery.OperatorActionRecord(1, self.operation, 3,
            'update', 'continue_recovery', recovery._timestamp(), self.grant['canonical_evidence_identity'],
            'planned', None, None))
        self.peer = self.enterContext(patch.object(managed_update, 'require_operation_entry'))

    @contextmanager
    def locked(self):
        descriptor = state._open_locked_file(state.lifecycle_paths(self.root).controller_lock_path, nonblocking=True)
        try:yield
        finally:fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)

    def normalize(self):
        with self.locked():update_recovery.prepare_transfer(self.root, self.operation, 3, 'continue_recovery')

    def test_fresh_permission_normalizes_only_generation_and_preserves_canonical_plan_and_original_permit(self):
        authority = state.inspect_state(self.root)
        before = update_controller.read_update_journal(self.canonical.path)
        permit = self.fixture.original_permit.read_bytes();database = self.canonical.database.read_bytes()
        self.normalize()
        after = update_controller.read_update_journal(self.canonical.path)
        self.assertEqual(after['lifecycle_generation'], 3)
        self.assertEqual({k: v for k, v in before.items() if k not in {'lifecycle_generation', 'updated_at'}},
            {k: v for k, v in after.items() if k not in {'lifecycle_generation', 'updated_at'}})
        self.assertEqual(state.inspect_state(self.root), authority)
        self.assertEqual(self.canonical.database.read_bytes(), database)
        self.assertEqual(self.fixture.original_permit.read_bytes(), permit)
        view = canonical_review(self.root, self.operation, control_manifest_sha256=self.fixture.fixture.fixture.digest)
        self.assertEqual(view['canonical_plan']['evidenceIdentity'], self.view['canonical_plan']['evidenceIdentity'])
        self.assertIsNone(observation.read_binding(self.root, self.operation)[3])
        raw = self.canonical.path.read_bytes();self.normalize()
        self.assertEqual(self.canonical.path.read_bytes(), raw)

    def test_missing_fresh_permission_or_publication_failure_preserves_original_gap_and_database(self):
        raw = self.canonical.path.read_bytes();authority = state.inspect_state(self.root)
        deadline = self.grant['expires_at_monotonic']
        self.grant['expires_at_monotonic'] = .001
        with self.assertRaises(ControlError):self.normalize()
        self.grant['expires_at_monotonic'] = deadline
        with patch.object(update_controller, '_private_atomic_write', side_effect=OSError('Injected publication failure')):
            with self.assertRaises(OSError):self.normalize()
        self.assertEqual(self.canonical.path.read_bytes(), raw)
        self.assertEqual(state.inspect_state(self.root), authority)

    def test_second_interrupted_transfer_remains_one_explainable_generation_without_losing_first_intent(self):
        self.normalize()
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=3)
        with patch.object(managed_update.os, 'getpid', return_value=self.canonical.main):
            lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=3, expected_phase='recovery_required', owner_capability=capability, next_owner_kind='updater')
        lease.release()
        authority, journal, _, witness = observation.read_binding(self.root, self.operation)
        self.assertEqual(authority.generation, 4);self.assertEqual(journal['lifecycle_generation'], 3)
        self.assertEqual(witness['source_generation'], 3)
        self.assertTrue(self.fixture.action_path.exists())
        self.assertTrue(self.fixture.original_permit.exists())
