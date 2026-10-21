from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import management_journal as contract, management_recovery_store as recovery, management_store, runtime_links as links, state
from tests.lifecycle.test_management_journal import _journal_payload, _execution_payload


class RuntimeLinkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('server', 'web', 'binding-state'):
            (self.root / name).mkdir(mode=0o700)
        for name in ('start.sh', 'update.sh'):
            (self.root / name).write_text('#!/bin/sh\n')
        self.paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(self.paths)
        state._write_state_unlocked(self.paths, state.LifecycleState(2, 0, None))
        payload = _journal_payload()
        payload.update(action='repair_environment', generation=1)
        candidate = deepcopy(payload['previous'])
        new = _execution_payload()
        for name in ('runtime_bundle_id', 'runtime_manifest_sha256', 'python_environment_id', 'python_executable',
                     'node_environment_id', 'node_executable', 'web_bundle_id', 'web_dependency_target', 'web_dist_target'):
            candidate[name] = new[name]
        payload['candidate'] = candidate
        self.original = contract.parse_management_journal(payload)
        self.lease, _ = state.claim_management_controller(self.root, operation_id=self.original.operation_id, expected_generation=0)
        self.addCleanup(lambda: self.lease.release())
        management_store.create_management_journal(self.root, self.original, lease=self.lease)
        previous = self.original
        for phase, stage in (('reserved', contract.ManagementStage.PREPARED), ('exclusive', contract.ManagementStage.EXCLUSIVE), ('verifying', contract.ManagementStage.VERIFYING)):
            if phase != 'reserved':
                state.transition_operation_with_credential(self.root, operation_id=previous.operation_id, generation=1,
                    expected_phase=contract.journal_phase(previous), next_phase=phase)
            current = replace(previous, stage=stage)
            management_store.advance_management_journal(self.root, previous, current, lease=self.lease)
            previous = current
        self.original = previous
        self.binding_state = self.root / 'binding-state'
        self.receipt = self.binding_state / 'runtime-links.json'
        self.receipt_payload = {'schema_version': 1, 'operation_id': self.original.operation_id, 'generation': 1,
            'previous': contract.execution_tuple_sha256(self.original.previous), 'candidate': contract.execution_tuple_sha256(self.original.candidate), 'stage': 'applying'}
        state._atomic_write_json(self.receipt, self.receipt_payload)
        (self.root / 'web/node_modules').symlink_to(self.original.candidate.web_dependency_target)
        (self.root / 'web/dist').symlink_to(self.original.previous.web_dist_target)
        self.layout_patch = patch.object(links, 'layout_for_root', return_value=SimpleNamespace(state=self.binding_state))
        self.layout_patch.start()
        self.addCleanup(self.layout_patch.stop)
        verify_patch = patch.object(links, 'verify_runtime')
        self.verify = verify_patch.start()
        self.addCleanup(verify_patch.stop)

    def recover(self, action='rollback'):
        generation = state.inspect_state(self.root).generation
        plan = recovery.inspect_recovery(self.root, self.original.operation_id, generation)
        self.lease.release()
        self.lease, _, current = recovery.acquire_recovery_lease(self.root, operation_id=self.original.operation_id,
            generation=generation, evidence_sha256=plan['evidence_sha256'], review_id=str(uuid.uuid4()), action=action,
            controller_invocation_id='2' * 32)
        return current

    def bind(self, journal, target='previous', checkpoint=None):
        return links.recover_management_links(self.root, journal, lease=self.lease, target=target,
            assert_children_stopped=checkpoint or Mock())

    def test_mixed_pair_rolls_back_together_and_retains_original_receipt(self):
        current = self.recover()
        raw = self.receipt.read_bytes()
        self.bind(current)
        links.verify_links(self.root, current.previous)
        self.assertEqual(self.receipt.read_bytes(), raw)
        evidence = self.binding_state / f'runtime-link-recovery-{current.operation_id}-{current.generation}.json'
        self.assertEqual(json.loads(evidence.read_text())['stage'], 'complete')
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.bind(current)
        self.verify.assert_called_with(self.root, current.previous)

    def test_cancel_between_pair_replacements_is_recoverable_under_same_review(self):
        current = self.recover(action='continue_recovery')
        checkpoint = Mock(side_effect=[None, None, RuntimeError('stop fixture')])
        with self.assertRaisesRegex(RuntimeError, 'stop fixture'):
            self.bind(current, target='candidate', checkpoint=checkpoint)
        self.assertEqual(os.readlink(self.root / 'web/node_modules'), current.candidate.web_dependency_target)
        self.assertEqual(os.readlink(self.root / 'web/dist'), current.previous.web_dist_target)
        self.bind(current, target='candidate')
        links.verify_links(self.root, current.candidate)

    def test_crash_after_first_recovery_link_can_be_followed_by_a_new_review(self):
        # Failure before the original transaction ever wrote a binding receipt:
        # only the prior completed pair exists. Recovery must journal its own
        # mutation before replacing either link.
        original = {**self.receipt_payload, 'operation_id': str(uuid.uuid4()), 'generation': 0,
                    'previous': None, 'candidate': contract.execution_tuple_sha256(self.original.previous), 'stage': 'complete'}
        state._atomic_write_json(self.receipt, original)
        (self.root / 'web/node_modules').unlink()
        (self.root / 'web/node_modules').symlink_to(self.original.previous.web_dependency_target)
        current = self.recover(action='continue_recovery')
        checkpoint = Mock(side_effect=[None, None, RuntimeError('actor died')])
        with self.assertRaisesRegex(RuntimeError, 'actor died'):
            self.bind(current, target='candidate', checkpoint=checkpoint)
        later = self.recover(action='rollback')
        self.bind(later)
        links.verify_links(self.root, later.previous)
        self.assertEqual(json.loads(self.receipt.read_text()), original)

    def test_foreign_pair_missing_link_or_wrong_receipt_never_mutates_bindings(self):
        current = self.recover()
        path = self.root / 'web/node_modules'
        for scenario in ('foreign', 'missing', 'receipt'):
            with self.subTest(scenario=scenario):
                path.unlink(missing_ok=True)
                path.symlink_to(current.candidate.web_dependency_target)
                state._atomic_write_json(self.receipt, self.receipt_payload)
                if scenario == 'foreign':
                    path.unlink()
                    path.symlink_to('/unrelated/node_modules')
                elif scenario == 'missing':
                    path.unlink()
                else:
                    state._atomic_write_json(self.receipt, {**self.receipt_payload, 'operation_id': str(uuid.uuid4())})
                before = os.readlink(self.root / 'web/dist')
                with self.assertRaises(links.RuntimeBuildError):
                    self.bind(current)
                self.assertEqual(os.readlink(self.root / 'web/dist'), before)

    def test_reviewed_target_cannot_be_changed_or_use_a_released_controller(self):
        current = self.recover()
        with self.assertRaises(links.RuntimeBuildError):
            self.bind(current, target='candidate')
        self.lease.release()
        with self.assertRaises(state.LifecycleError):
            self.bind(current)

    def test_accepted_work_never_restores_previous_bindings(self):
        self.lease.release()
        # The public guard rejects acceptance before any state/filesystem work.
        accepted = replace(self.original, stage=contract.ManagementStage.RECOVERY_REQUIRED,
            acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.original.candidate), 'a' * 64))
        with self.assertRaises(links.RuntimeBuildError):
            self.bind(accepted)


if __name__ == '__main__':
    unittest.main()
