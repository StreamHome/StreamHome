import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import management_journal as contract, state
from lifecycle.control import administration, workers
from lifecycle.control.requests import parse_management_request
from tests.lifecycle.test_management_requests import request_payload


class ManagementObserverTests(unittest.TestCase):
    def observe(self, stage, phase):
        reviewed=parse_management_request(request_payload())
        generation=reviewed.expected_generation+1
        journal=contract.ManagementJournal(reviewed.installation_id,reviewed.operation_id,generation,reviewed.action,
            reviewed.activation_mode,reviewed.review_id,reviewed.previous,reviewed.candidate,reviewed.initial_service_state,
            '1'*32,reviewed.supervisor_invocation_id,stage,None)
        authority=state.LifecycleState(2,generation,state.ActiveOperation(reviewed.operation_id,state.OperationType.MANAGEMENT,
            phase,state.OwnerKind.MANAGEMENT_CONTROLLER,'c'*64))
        lease=SimpleNamespace(pid=200,operation_id=reviewed.operation_id,generation=generation)
        with patch('lifecycle.control.requests.read_management_request',return_value=reviewed), patch('lifecycle.management_store.inspect_management_journal',return_value=journal), patch.object(state,'inspect_state',return_value=authority), patch.object(state,'read_controller_lease',return_value=lease), patch.object(state,'controller_lease_is_current',return_value=True), patch('lifecycle.control.processes.controller_peer'), patch.object(state,'validate_operation_with_credential',return_value=authority) as validate:
            result=workers.bridge_observe({'operation_id':reviewed.operation_id,'controller_pid':200,'controller_invocation_id':'1'*32})
        return result,validate

    def test_exclusive_progress_does_not_wait_for_controller_state_writer_lock(self):
        result,validate=self.observe(contract.ManagementStage.EXCLUSIVE,state.LifecyclePhase.EXCLUSIVE)
        self.assertFalse(result['os_effect_requested'])
        validate.assert_not_called()

    def test_os_effect_request_still_requires_current_prepared_credential(self):
        result,validate=self.observe(contract.ManagementStage.PREPARED,state.LifecyclePhase.RESERVED)
        self.assertTrue(result['os_effect_requested'])
        self.assertEqual(validate.call_count,1)
        self.assertEqual(validate.call_args.kwargs['expected_phase'],state.LifecyclePhase.RESERVED)

    def test_observer_timeout_is_reobserved_without_restart_or_os_effect(self):
        operation=str(uuid.uuid4())
        active={'ActiveState':'active','MainPID':'200','InvocationID':'1'*32}
        ended={**active,'ActiveState':'inactive','MainPID':'0'}
        with patch.object(administration,'require_root'), patch.object(administration,'publish_request'), patch.object(administration,'operation_unit',side_effect=[active,ended]), patch.object(administration,'run_worker',side_effect=[subprocess.TimeoutExpired('isolated-observer',30),{'completed':True,'stage':'succeeded'}]), patch.object(administration,'_apply_fixed_boot_effect') as effect, patch.object(administration,'systemctl') as systemctl, patch.object(administration.time,'sleep'):
            result=administration.run_operation({'operation_id':operation},start=False)
        self.assertEqual(result,{'completed':True,'stage':'succeeded'})
        effect.assert_not_called()
        systemctl.assert_not_called()


if __name__ == '__main__':
    unittest.main()
