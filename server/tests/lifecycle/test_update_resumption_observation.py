"""Actual canonical UPDATE transfer with explicit substituted OS/build observations."""
import json
import os
import unittest
from unittest.mock import patch
import uuid

from lifecycle import managed_update, recovery, state, update_controller
from lifecycle.control import update_grants
from lifecycle.control import update_resumption_observation as observe
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture
from tests.lifecycle.test_resumption_contract import plan


class UpdateResumptionObservationTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.fixture.operation
        fixture.snapshot['runtime_status'] = 'offline'
        fixture.payload['resumption_plan_id'] = str(uuid.uuid4())
        self.grant = fixture.prepare()
        self.plan = {**plan(), 'schema_version': 2, 'owner_type': 'update', 'action': 'update_recovery',
            'plan_id': self.grant['resumption_plan_id'], 'installation_id': self.grant['installation_id'],
            'operation_id': self.operation, 'review_id': self.grant['review_id'], 'generation': 3,
            'parent_binding_sha256': self.grant['parent_binding_sha256'], 'recovery_action': 'continue_recovery',
            'control_manifest_sha256': self.grant['control_manifest_sha256'],
            'stages': [{'kind': 'candidate_start', 'execution_sha256': self.grant['accepted_execution_sha256'],
                'generation': 3, 'handoff_sha256': None}]}
        self.record = {'plan': self.plan, 'issuer': {'scope': self.grant['issuer_scope']}}
        self.enterContext(patch.object(observe, 'ROOT', self.root))
        self.enterContext(patch.object(update_grants, 'read_grant', return_value=self.grant))
        self.enterContext(patch('lifecycle.control.resumption_ticket.reviewed_plan', return_value=self.record))
        self.main = fixture.fixture.fixture.main
        self.actor = {'ActiveState': 'active', 'MainPID': str(self.main), 'InvocationID': 'b'*32}
        self.actor_read = self.enterContext(patch.object(observe, 'operation_unit', side_effect=lambda op: self.actor))
        self.owner = self.enterContext(patch.object(observe, 'require_owner_peer', side_effect=lambda root, lease, pid:
            managed_update.bound_journal(root, lease.operation_id)))
        self.quiet = self.enterContext(patch.object(observe, 'require_service_quiescent'))
        self.delegate = self.enterContext(patch.object(managed_update, 'quiescent_delegate',
            return_value=(self.main+10, '111', self.main+11, '112')))
        self.runtime = self.enterContext(patch.object(observe, 'verify_runtime'))
        self.links = self.enterContext(patch.object(observe, 'verify_links'))
        self.payload = {'plan': self.plan, 'kind': 'candidate_start', 'controller_pid': self.main,
            'controller_invocation_id': 'b'*32}
        self.lease = None
        self.addCleanup(lambda: self.lease.release() if self.lease is not None and not self.lease._released else None)

    def ready(self):
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        with patch.object(managed_update.os, 'getpid', return_value=self.main):
            self.lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=2, expected_phase='recovery_required', owner_capability=capability, next_owner_kind='updater')
        update_controller._record_transferred_generation(self.root, operation_id=self.operation,
            previous_generation=2, generation=3, lifecycle_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
            target_commit=self.fixture.journal['target_commit'], previous_commit=self.fixture.journal['previous_commit'])
        for old, new in (('recovery_required', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=3,
                expected_phase=old, next_phase=new)
        path = self.fixture.fixture.fixture.path
        journal = update_controller.read_update_journal(path)
        journal.update(lifecycle_phase='verifying', phase='recovering')
        path.write_text(json.dumps(journal))
        self.paths = recovery._new_action_paths(self.root, self.operation, 2, action_id=self.grant['review_id'])
        recovery._write_action_record(self.paths.record_path, recovery.OperatorActionRecord(1, self.operation, 2,
            'update', 'continue_recovery', recovery._timestamp(), self.grant['canonical_evidence_identity'],
            'executing', 3, None))

    def test_pre_verifying_observation_is_pending_and_does_not_verify_or_start_runtime(self):
        before = state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()
        self.assertEqual(observe.observe(self.payload), {'pending': True})
        self.runtime.assert_not_called();self.quiet.assert_not_called();self.owner.assert_not_called()
        self.assertEqual((state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()), before)

    def test_exact_transferred_accepted_previous_tuple_produces_only_reviewed_start_observation(self):
        self.ready()
        before = state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()
        result = observe.observe(self.payload)
        self.assertEqual(result['owner_type'], 'update');self.assertIs(result['accepted'], True)
        self.assertEqual(result['execution_sha256'], self.grant['accepted_execution_sha256'])
        self.assertEqual(result['generation'], 3)
        self.runtime.assert_called_once_with(self.root, self.fixture.bound.accepted)
        self.links.assert_called_once_with(self.root, self.fixture.bound.accepted)
        self.assertEqual((state.inspect_state(self.root), self.fixture.fixture.fixture.path.read_bytes()), before)

    def test_foreign_unit_missing_intent_changed_plan_and_unproven_quiescence_refuse_observation(self):
        self.ready()
        self.actor['InvocationID'] = 'c'*32
        with self.assertRaises(ControlError):observe.observe(self.payload)
        self.actor['InvocationID'] = 'b'*32
        with patch.object(observe, 'require_service_quiescent', side_effect=ControlError('children remain')):
            with self.assertRaises(ControlError):observe.observe(self.payload)
        original = self.paths.record_path.read_bytes()
        action = json.loads(original);action['requested_action'] = 'safe_abort'
        self.paths.record_path.write_text(json.dumps(action))
        with self.assertRaises(ControlError):observe.observe(self.payload)
        self.paths.record_path.write_bytes(original)
        changed = {**self.plan, 'owner_type': 'restore', 'action': 'restore_recovery'}
        with self.assertRaises(ControlError):observe.observe({**self.payload, 'plan': changed})
