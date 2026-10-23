from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import parent_execution as binding, state
from lifecycle.management_journal import parse_execution_tuple, execution_tuple_sha256
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_management_journal import _execution_payload


class ParentExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'server').mkdir()
        for name in ('start.sh','update.sh'):(self.root/name).write_text('#!/bin/sh\n')
        self.claim = state.claim_operation_with_credential(self.root, operation_type=state.OperationType.RESTORE,
                                                           owner_kind=state.OwnerKind.RESTORE_CONTROLLER)
        self.execution = parse_execution_tuple(_execution_payload())
        self.installation = str(uuid.uuid4())
        self.descriptor = SimpleNamespace(service_uid=os.geteuid(), installation_id=self.installation)
        self.layout = SimpleNamespace(managed=True, descriptor=SimpleNamespace(descriptor=self.descriptor))
        self.active = SimpleNamespace(installation_id=self.installation, execution=self.execution)
        self.supervisor = {'pid':os.getpid(), 'start_identity':state._process_start_identity(os.getpid()),
            'invocation':'a'*32, 'launches_open':True, 'execution_sha256':execution_tuple_sha256(self.execution),
            'children':{'backend':{'pid':os.getpid(), 'start_identity':state._process_start_identity(os.getpid())}}}
        self.enterContext(patch.object(binding,'layout_for_root',return_value=self.layout))
        self.enterContext(patch.object(binding,'read_active_execution',return_value=self.active))
        self.enterContext(patch('lifecycle.control.processes.process_cgroup',return_value='/system.slice/streamhome.service'))
        self.enterContext(patch('lifecycle.control.privileged.supervisor_snapshot',return_value=self.supervisor))
        self.unit=self.enterContext(patch('lifecycle.control.privileged.unit_snapshot',return_value={'InvocationID':'a'*32}))
        self.epoch=self.enterContext(patch('lifecycle.control.tickets.read_stop_epoch',return_value='b'*64))
        self.enterContext(patch.dict(os.environ, {'INVOCATION_ID':'a'*32}))

    def capture(self,**changes):
        arguments={'operation_id':self.claim.state.active_operation.operation_id,
                   'generation':self.claim.state.generation,'operation_type':'restore'}
        return binding.capture(self.root,**{**arguments,**changes})

    def test_exact_existing_claim_is_captured_without_rotating_or_completing_authority(self):
        before=state.inspect_state(self.root);record=self.capture()
        self.assertEqual(binding.parse_parent_execution(record.payload()),record)
        self.assertEqual(record.execution,self.execution)
        self.assertEqual(record.operation_id,before.active_operation.operation_id)
        self.assertEqual(state.inspect_state(self.root),before)

    def test_wrong_owner_or_replaced_backend_never_invents_a_binding(self):
        with self.assertRaises(ControlError):self.capture(operation_type='update')
        self.supervisor['children']['backend']['pid']=os.getpid()+1
        with self.assertRaises(ControlError):self.capture()

    def test_stop_or_supervisor_change_during_capture_is_refused(self):
        self.epoch.side_effect=['b'*64,'c'*64]
        with self.assertRaises(ControlError):self.capture()
        self.epoch.side_effect=None
        self.unit.side_effect=[{'InvocationID':'a'*32},{'InvocationID':'c'*32}]
        with self.assertRaises(ControlError):self.capture()

    def test_record_parser_requires_original_execution_and_strict_typed_identity(self):
        original=self.capture().payload()
        for value in ({**original,'schema_version':True},{**original,'original_generation':True},
                      {**original,'supervisor_pid':True},{**original,'operation_type':'management'},
                      {**original,'supervisor_invocation_id':'0'*32},{**original,'execution':None},
                      {**original,'owner_capability':'forbidden'}):
            with self.subTest(value=value),self.assertRaises(ControlError):binding.parse_parent_execution(value)


if __name__=='__main__':unittest.main()
