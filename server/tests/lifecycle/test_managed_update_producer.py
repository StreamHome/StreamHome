"""Managed queue/cancel and failed publication with actual state, files and RAM admission.

Supervisor/kernel and complete-reader support are explicit substituted boundaries;
this does not advertise or qualify a native managed UPDATE reader.
"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from lifecycle import runtime_admission, state, update_execution
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_continuation as continuation_fixture


class ManagedUpdateProducerTests(unittest.TestCase):
    def setUp(self):
        from services import update
        fixture = continuation_fixture.UpdateContinuationTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture;self.update = update
        claimed = state.inspect_state(fixture.root)
        fixture.admission.note_update_claim(fixture.operation);fixture.path.unlink()
        fixture.admission.abort_unjournaled_update_claim(claimed)
        self.assertTrue(fixture.admission.admitted())
        for name, value in {'WORKSPACE_ROOT': fixture.root, 'RUN_DIR': fixture.path.parent,
                'STATUS_PATH': fixture.path, 'STATUS_LOCK_PATH': fixture.path.with_name('update-state.lock'),
                'LOG_PATH': fixture.path.with_name('update.log')}.items():
            self.enterContext(patch.object(update, name, value))
        self.enterContext(patch.object(update, 'layout_for_root', return_value=fixture.layout))
        self.enterContext(patch.object(runtime_admission, 'require_runtime_admission', return_value=fixture.admission))
        self.enterContext(patch.object(update, 'current_commit', new=AsyncMock(return_value='a'*40)))
        def capture(root, **kwargs):
            intent = fixture.admission._pending_update_claim
            self.assertIsNotNone(intent)
            self.assertEqual(kwargs, {'operation_id': intent.parent.operation_id,
                'generation': intent.authority.generation, 'operation_type': 'update'})
            return intent.parent
        self.capture = self.enterContext(patch('lifecycle.parent_execution.capture', side_effect=capture))
        initial = {**update._default_status(), 'phase': 'update_available', 'update_available': True,
            'current_commit': 'a'*40, 'target_commit': 'e'*40}
        state._atomic_write_json(fixture.path, initial)

    def queue(self, *, automatic=False):
        return asyncio.run(self.update.queue_update(automatic=automatic))

    def test_queue_persists_original_complete_binding_before_admitting_only_existing_runtime(self):
        result = self.queue()
        authority = state.inspect_state(self.fixture.root)
        bound = update_execution.parse(result)
        self.assertEqual(authority.generation, 2)
        self.assertEqual(bound.parent.operation_id, authority.active_operation.operation_id)
        self.assertEqual(bound.parent.original_generation, 2)
        self.assertIsNone(bound.candidate);self.assertIsNone(bound.accepted)
        self.assertTrue(self.fixture.admission.admitted())
        self.assertEqual(self.fixture.admission.generation, 1)
        self.assertEqual(self.fixture.context.generation, 0)

    def test_current_restore_only_reader_refuses_before_any_new_claim_or_publication(self):
        before = state.inspect_state(self.fixture.root), self.fixture.path.read_bytes()
        with patch('lifecycle.control.reader_support.require_managed_update_reader',
                side_effect=ControlError('control_bound_reader_incompatible')):
            with self.assertRaisesRegex(RuntimeError, 'managed_update_unavailable'):self.queue()
        self.capture.assert_not_called()
        self.assertEqual(before, (state.inspect_state(self.fixture.root), self.fixture.path.read_bytes()))
        self.assertTrue(self.fixture.admission.admitted())

    def test_capture_or_prepublication_failure_releases_only_known_unadopted_claim(self):
        self.capture.side_effect = OSError('Injected supervisor capture failure')
        original = self.fixture.path.read_bytes()
        with self.assertRaises(OSError):self.queue()
        self.assertIsNone(state.inspect_state(self.fixture.root).active_operation)
        self.assertEqual(state.inspect_state(self.fixture.root).generation, 2)
        self.assertEqual(self.fixture.path.read_bytes(), original)
        self.assertEqual(self.fixture.admission.generation, 2)
        self.assertTrue(self.fixture.admission.admitted())

    def test_indeterminate_publication_keeps_adopted_claim_and_original_binding(self):
        write = self.update.write_update_state
        def uncertain(**changes):
            write(**changes)
            raise OSError('Injected post-publication failure')
        with patch.object(self.update, 'write_update_state', side_effect=uncertain):
            with self.assertRaises(OSError):self.queue()
        authority = state.inspect_state(self.fixture.root)
        bound = update_execution.parse(json.loads(self.fixture.path.read_text()))
        self.assertIsNotNone(authority.active_operation)
        self.assertEqual(bound.parent.operation_id, authority.active_operation.operation_id)
        self.assertEqual(authority.generation, 2)
        self.assertFalse(self.fixture.admission.admitted())

    def test_cancel_retains_original_automatic_request_and_settles_same_live_runtime(self):
        queued = self.queue(automatic=True)
        cancelled = asyncio.run(self.update.cancel_queued_update())
        self.assertEqual(cancelled['phase'], 'update_available')
        self.assertTrue(cancelled['automatic'])
        self.assertIsNone(state.inspect_state(self.fixture.root).active_operation)
        self.assertEqual(self.fixture.admission.generation, 2)
        self.assertTrue(self.fixture.admission.admitted())
        from lifecycle.update_history import _path
        record = json.loads(_path(self.fixture.root, queued['transaction_id'], queued['lifecycle_generation']).read_text())
        self.assertEqual(record['journal'], cancelled)
        self.assertEqual(update_execution.parse(record['journal']).parent, update_execution.parse(queued).parent)

    def test_managed_unbound_legacy_status_cannot_create_an_inferred_parent_or_cleanup(self):
        before = state.inspect_state(self.fixture.root), self.fixture.path.read_bytes()
        status = json.loads(self.fixture.path.read_text())
        with self.assertRaisesRegex(RuntimeError, 'managed_update_unbound'):
            self.update._ensure_update_lifecycle(status)
        with self.assertRaisesRegex(RuntimeError, 'managed_update_recovery_required'):
            self.update.cleanup_reconciled_transaction(status)
        self.assertFalse(asyncio.run(self.update.reconcile_orphaned_update()))
        self.assertEqual(before, (state.inspect_state(self.fixture.root), self.fixture.path.read_bytes()))
