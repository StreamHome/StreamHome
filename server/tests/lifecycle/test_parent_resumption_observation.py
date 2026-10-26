import os
import unittest
from unittest.mock import patch

from lifecycle import recovery,restore_controller as restore,state
from lifecycle.control import parent_resumption_observation as observe
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_parent_resumption_runtime as fixtures


class ParentResumptionObservationTests(unittest.TestCase):
    def setUp(self):
        fixtures.ParentRuntimeEnrollmentTests.setUp(self)
        self.record_paths=recovery._new_action_paths(self.root,self.operation,2)
        self.record=recovery.OperatorActionRecord(1,self.operation,2,'restore','continue_recovery',
            '2026-10-23T12:00:00Z',self.grant['canonical_evidence_identity'],'executing',3,None)
        recovery._write_action_record(self.record_paths.record_path,self.record)
        self.enterContext(patch.object(observe,'ROOT',self.root))
        self.journal_reader=self.enterContext(patch.object(observe,'bound_journal',side_effect=lambda root,op:(
            restore.inspect_restore_journal(self.root,op),self.bound)))
        self.actor={'ActiveState':'active','MainPID':str(os.getpid()),'InvocationID':'b'*32}
        self.actor_reader=self.enterContext(patch.object(observe,'operation_unit',return_value=self.actor))
        self.enterContext(patch.object(observe,'controller_peer'))
        self.quiet=self.enterContext(patch.object(observe,'require_service_quiescent'))
        self.verify=self.enterContext(patch.object(observe,'verify_runtime'))
        self.links=self.enterContext(patch.object(observe,'verify_links'))
        self.payload={'plan':self.plan,'kind':'candidate_start','controller_pid':os.getpid(),
                      'controller_invocation_id':'b'*32}

    def test_existing_parent_and_operator_receipt_are_observed_before_one_start_without_mutation(self):
        before=state.inspect_state(self.root);journal=restore.inspect_restore_journal(self.root,self.operation)
        value=observe.observe(self.payload)
        self.assertTrue(value['accepted']);self.assertEqual(value['owner_type'],'restore')
        self.assertEqual(value['generation'],3);self.assertEqual(value['parent_binding_sha256'],self.grant['parent_binding_sha256'])
        self.quiet.assert_called_once_with(inspections=True,settle_timeout=2,controller=(self.operation,os.getpid(),self.lease.lease.process_start_identity))
        self.verify.assert_called_once_with(self.root,self.bound.execution)
        self.links.assert_called_once_with(self.root,self.bound.execution)
        self.assertEqual(state.inspect_state(self.root),before)
        self.assertEqual(restore.inspect_restore_journal(self.root,self.operation),journal)

    def test_exact_pending_parent_is_distinct_from_start_authority_and_is_read_only(self):
        state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=3,
            expected_phase='verifying',next_phase='recovery_required')
        before=state.inspect_state(self.root);journal=restore.inspect_restore_journal(self.root,self.operation)
        self.assertEqual(observe.observe(self.payload),{'pending':True})
        self.quiet.assert_not_called();self.verify.assert_not_called();self.links.assert_not_called()
        self.assertEqual(state.inspect_state(self.root),before)
        self.assertEqual(restore.inspect_restore_journal(self.root,self.operation),journal)
        self.grant['parent_binding_sha256']='f'*64
        with self.assertRaises(ControlError):observe.observe(self.payload)

    def test_controller_invocation_generation_or_missing_quiescence_never_authorize_start(self):
        self.actor_reader.return_value={**self.actor,'InvocationID':'f'*32}
        with self.assertRaises(ControlError):observe.observe(self.payload)
        self.actor_reader.return_value=self.actor
        self.quiet.side_effect=ControlError('control_foreign_service_process')
        with self.assertRaises(ControlError):observe.observe(self.payload)
        self.verify.assert_not_called()

    def test_runtime_or_authority_changed_during_verification_is_refused(self):
        def change(*args):
            state.transition_operation_with_credential(self.root,operation_id=self.operation,generation=3,
                expected_phase='verifying',next_phase='recovery_required')
        self.verify.side_effect=change
        with self.assertRaises(ControlError):observe.observe(self.payload)
        self.assertEqual(state.inspect_state(self.root).generation,3)

    def test_startup_hook_is_forwarded_only_to_the_existing_strict_os_quiescence_checker(self):
        observe.observe({**self.payload,'startup_hook':{'pid':4321,'start_identity':'123'}})
        self.assertEqual(self.quiet.call_args.kwargs['startup_hook'],(4321,'123'))
        with self.assertRaises(ControlError):observe.observe({**self.payload,'startup_hook':{'command':'anything'}})


if __name__=='__main__':unittest.main()
