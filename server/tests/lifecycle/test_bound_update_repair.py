"""Original UPDATE repair custody; root OS and artifact reconstruction are explicit boundaries."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_update_repair as repair, managed_update, state, update_controller, update_execution
from lifecycle import update_repair_intents as intents, update_recovery_observation
from lifecycle.control import update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture


class BoundUpdateRepairTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.fixture.operation;self.canonical = fixture.fixture.fixture
        self.enterContext(patch.object(repair, 'layout_for_root', return_value=self.canonical.layout))
        self.enterContext(patch.object(update_controller, 'layout_for_root', return_value=self.canonical.layout))
        self.enterContext(patch.object(update_recovery_observation, 'layout_for_root', return_value=self.canonical.layout))
        self.enterContext(patch.object(repair, 'read_stop_epoch', return_value=self.canonical.parent.stop_epoch))
        self.stopped = self.enterContext(patch.object(repair, 'require_stopped'))
        self.quiet = self.enterContext(patch.object(repair, 'require_service_quiescent'))
        self.enterContext(patch.object(managed_update, 'require_operation_entry'))
        self.enterContext(patch.object(update_grants, 'require_live_identity'))
        self.inventory = {'manifest_sha256': self.canonical.parent.execution.runtime_manifest_sha256,
            'filesystem_sha256': '8'*64, 'targets': {name: '9'*64 for name in ('python', 'node', 'web', 'bundles')}}
        self.inspect = self.enterContext(patch.object(repair.artifacts, 'inspect', return_value=self.inventory))
        self.reconstruct = self.enterContext(patch.object(repair.artifacts, 'reconstruct', side_effect=self.rebuild))
        self.grants = {}
        self.enterContext(patch.object(update_grants, 'read_grant', side_effect=lambda operation, generation, **kwargs:
            self.grants[generation]))
        self.enterContext(patch.object(update_grants, 'grant_path', side_effect=lambda operation, generation:
            self.root/('protected-root-path-'+str(generation))))
        self.manifest = self.canonical.parent.execution.control_manifest_sha256
        self.issue()

    def issue(self):
        self.view = repair.plan(self.root, self.operation, control_manifest_sha256=self.manifest)
        self.payload = {'operation_id': self.operation,
            'recovery_review_id': str(uuid.uuid4()), 'expected_evidence_sha256': self.view['evidence_sha256'],
            'issuer_scope': self.fixture.scope, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'stop_epoch': self.view['stop_epoch']}
        self.grant = repair.prepare(self.root, self.payload, control_manifest_sha256=self.manifest)
        self.grants[self.view['generation']+1] = self.grant

    def rebuild(self, root, cache, selected, **kwargs):
        kwargs['checkpoint']();kwargs['quiescent']()
        record = {'status': 'complete', 'manifest_sha256': selected.runtime_manifest_sha256,
            'filesystem_sha256': kwargs['expected']['filesystem_sha256']}
        state._atomic_write_json(kwargs['receipt'], record)
        return record

    def execute(self):
        with patch.object(managed_update.os, 'getpid', return_value=self.canonical.main):
            return repair.BoundUpdateEnvironmentController(self.root, self.operation,
                control_manifest_sha256=self.manifest).execute()

    def test_readonly_plan_selects_exact_accepted_previous_tuple_and_permission_cannot_authorize_shell_progress(self):
        before = state.inspect_state(self.root), self.canonical.path.read_bytes(), self.canonical.database.read_bytes()
        self.assertEqual(self.view['selected_execution_sha256'], self.grant['accepted_execution_sha256'])
        self.assertEqual(self.grant['schema_version'], 3)
        self.assertEqual(self.grant['action'], 'repair_bound_environment')
        self.assertEqual((state.inspect_state(self.root), self.canonical.path.read_bytes(), self.canonical.database.read_bytes()), before)
        with self.assertRaises(ControlError):update_grants.parse_grant({**self.grant, 'action': 'continue_recovery'})
        with self.assertRaises(ControlError):update_grants.parse_grant({**self.grant, 'repair_execution_sha256': self.grant['candidate_execution_sha256']})
        with patch('lifecycle.control.update_resumption_helpers.unit_snapshot', side_effect=AssertionError('No runtime action')):
            with self.assertRaises(ControlError):managed_update.require_delegate_peer(self.root,
                self.canonical.lease.lease, self.canonical.helper)

    def test_actual_original_controller_transfer_repairs_only_artifacts_and_leaves_accepted_parent_stopped_rr(self):
        original = update_controller.read_update_journal(self.canonical.path)
        before = self.canonical.database.read_bytes(), self.fixture.fixture.active(), self.canonical.bound()
        with patch.object(update_controller, '_materialize_target_controller', side_effect=AssertionError('No shell engine')):
            self.assertEqual(self.execute(), 0)
        current = state.inspect_state(self.root)
        self.assertEqual(current.generation, 3);self.assertEqual(current.active_operation.operation_id, self.operation)
        self.assertEqual(current.active_operation.phase.value, 'recovery_required')
        after = update_controller.read_update_journal(self.canonical.path)
        self.assertEqual({k:v for k,v in original.items() if k not in {'lifecycle_generation','updated_at'}},
            {k:v for k,v in after.items() if k not in {'lifecycle_generation','updated_at'}})
        self.assertEqual((self.canonical.database.read_bytes(), self.fixture.fixture.active(), self.canonical.bound()), before)
        with self.assertRaises(update_controller.UpdateControllerConflict):
            update_controller._require_managed_progress_permission(self.root, after)
        with patch('lifecycle.runtime_build.verify_runtime') as verify:
            result = repair.result(self.root, self.operation, control_manifest_sha256=self.manifest)
        self.assertIs(result['completed'], True)
        verify.assert_called_once_with(self.root, before[2].accepted)
        self.reconstruct.assert_called_once()

    def test_interrupted_transfer_is_readonly_witnessed_and_fresh_repair_preserves_first_intent(self):
        with patch.object(update_controller, '_record_transferred_generation', side_effect=OSError('Injected after ownership CAS')):
            with self.assertRaises(OSError):self.execute()
        first_grant = self.grant;first = intents.path(self.root, first_grant);retained = first.read_bytes()
        authority, journal, _, witness = update_recovery_observation.read_binding(self.root, self.operation)
        self.assertEqual(authority.generation, 3);self.assertEqual(journal['lifecycle_generation'], 2)
        self.assertIs(witness['repair_transfer'], True)
        with patch.object(update_grants.time, 'monotonic', return_value=first_grant['expires_at_monotonic']+1):
            self.issue()
            self.assertEqual(self.execute(), 0)
        self.assertEqual(first.read_bytes(), retained)
        self.assertEqual(state.inspect_state(self.root).generation, 4)

    def test_stale_artifact_inventory_or_unproven_stopped_state_cannot_transfer(self):
        before = state.inspect_state(self.root), self.canonical.path.read_bytes()
        self.inspect.return_value = {**self.inventory, 'filesystem_sha256': 'f'*64}
        with self.assertRaises(ControlError):self.execute()
        self.assertEqual((state.inspect_state(self.root), self.canonical.path.read_bytes()), before)
        self.inspect.return_value = self.inventory;self.stopped.side_effect = ControlError('service remains active')
        with self.assertRaises(ControlError):self.execute()
        self.assertEqual((state.inspect_state(self.root), self.canonical.path.read_bytes()), before)

    def test_fixed_operation_dispatch_selects_only_original_update_maintenance_adapter(self):
        path = update_grants.grant_path(self.operation, 3);path.write_text('Protected-path access boundary')
        with (patch.object(managed_update.os, 'getpid', return_value=self.canonical.main),
                patch('lifecycle.update_recovery.execute', side_effect=AssertionError('No forward recovery')),
                patch.object(managed_update, 'require_initial_dispatch', side_effect=AssertionError('No new initial claim'))):
            self.assertEqual(managed_update.execute(self.root, self.operation, control_manifest_sha256=self.manifest), 0)
        self.assertEqual(state.inspect_state(self.root).active_operation.phase.value, 'recovery_required')
        self.assertEqual(state.inspect_state(self.root).generation, 3)

    def test_partial_reconstruction_requires_fresh_review_and_preserves_prior_receipt(self):
        receipts = []
        def fail(root, cache, selected, **kwargs):
            record = {'status': 'reconstructing', 'manifest_sha256': selected.runtime_manifest_sha256,
                'filesystem_sha256': kwargs['expected']['filesystem_sha256']}
            state._atomic_write_json(kwargs['receipt'], record);receipts.append(kwargs['receipt'])
            raise OSError('Injected partial cached artifact reconstruction')
        self.reconstruct.side_effect = fail
        original = self.canonical.bound();database = self.canonical.database.read_bytes()
        with self.assertRaises(OSError):self.execute()
        first = receipts[0].read_bytes()
        self.inventory = {**self.inventory, 'filesystem_sha256': 'a'*64}
        self.inspect.return_value = self.inventory;self.reconstruct.side_effect = self.rebuild
        self.issue();self.assertEqual(self.execute(), 0)
        self.assertEqual(receipts[0].read_bytes(), first)
        self.assertEqual(self.canonical.bound(), original);self.assertEqual(self.canonical.database.read_bytes(), database)
        self.assertEqual(state.inspect_state(self.root).generation, 4)
