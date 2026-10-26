from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import managed_restore,restore_controller as restore,state
from lifecycle.layout import layout_for_root
from lifecycle.management_journal import parse_execution_tuple
from lifecycle.parent_execution import ParentExecution
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_management_journal import _execution_payload


class ManagedRestoreJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        (self.root/'server').mkdir()
        for name in ('start.sh','update.sh'):(self.root/name).write_text('#!/bin/sh\n')
        self.claim=state.claim_operation_with_credential(self.root,operation_type='restore',owner_kind='restore_controller')
        self.operation=self.claim.state.active_operation.operation_id
        self.bound=ParentExecution(str(uuid.uuid4()),self.operation,'restore',1,
            parse_execution_tuple(_execution_payload()),'a'*32,os.getpid(),state._process_start_identity(os.getpid()),
            str(uuid.uuid4()),'b'*64)
        self.candidate=restore.CandidateIdentity('backup_20260101_000000.db',128,'c'*64)
        original=layout_for_root(self.root)
        self.layout=replace(original,descriptor=SimpleNamespace(descriptor=SimpleNamespace(
            installation_id=self.bound.installation_id,service_uid=os.geteuid())))
        self.enterContext(patch('lifecycle.layout.layout_for_root',return_value=self.layout))
        self.enterContext(patch('lifecycle.parent_execution.capture',return_value=self.bound))
        self.reader=self.enterContext(patch('lifecycle.control.reader_support.require_managed_restore_reader'))

    def create(self):
        return restore.create_restore_journal(self.root,operation_id=self.operation,generation=1,candidate=self.candidate)

    def test_managed_journal_binds_original_execution_and_never_synthesizes_it_for_legacy(self):
        journal=self.create()
        self.assertEqual(journal['schema_version'],2)
        self.assertEqual(journal['managed_execution'],self.bound.payload())
        self.reader.assert_called_once_with(self.bound.execution)
        for value in ({**journal,'schema_version':1},{**journal,'managed_execution':None},
                      {**journal,'managed_execution':replace(self.bound,original_generation=2).payload()}):
            with self.assertRaises(restore.RestorePersistenceError):
                restore._validate_journal(value,restore.restore_paths(self.root,self.operation))

    def test_incompatible_control_reader_prevents_journal_publication(self):
        self.reader.side_effect=ControlError('control_bound_reader_incompatible')
        with self.assertRaises(ControlError):self.create()
        self.assertFalse(restore.restore_paths(self.root,self.operation).journal_path.exists())
        self.assertEqual(state.inspect_state(self.root).generation,1)

    def test_binding_cannot_change_through_normal_progress_updates_or_schema_downgrade(self):
        self.create()
        with self.assertRaises(restore.RestorePersistenceError):
            restore.update_restore_journal(self.root,self.operation,
                managed_execution=replace(self.bound,stop_epoch='d'*64).payload())
        self.assertEqual(restore.read_restore_journal(self.root,self.operation)['managed_execution'],self.bound.payload())
        with self.assertRaises(restore.RestorePersistenceError):
            restore.update_restore_journal(self.root,self.operation,schema_version=1)

    def test_real_takeover_retains_binding_and_response_eof_stays_recovery_required(self):
        self.create()
        ack_read,ack_write=os.pipe();gate_read,gate_write=os.pipe();os.close(gate_write)
        self.addCleanup(os.close,ack_read)
        with patch.object(restore,'_stop_supported_runtime') as stop:
            self.assertEqual(restore._run_controller(self.root,self.operation,1,self.claim.owner_capability,
                acknowledgement_fd=ack_write,gate_fd=gate_read),1)
            stop.assert_not_called()
        journal=restore.read_restore_journal(self.root,self.operation)
        self.assertEqual(journal['generation'],2)
        self.assertEqual(journal['managed_execution'],self.bound.payload())
        self.assertFalse(journal['accepted'])
        self.assertEqual(state.inspect_state(self.root).active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)

    def test_managed_runtime_helpers_delegate_to_supervisor_and_never_spawn_app_children(self):
        self.create()
        self.enterContext(patch('lifecycle.managed_restore.resumption_offline',return_value=False))
        with patch('lifecycle.managed_restore.command',return_value={'stopped':True}) as command,patch.object(restore.subprocess,'Popen') as spawn:
            restore._stop_supported_runtime(self.root,3000)
            command.assert_called_once_with(self.root,'restore_stop_children');spawn.assert_not_called()
        with patch('lifecycle.managed_restore.command',return_value={'maintenance':True}) as command,patch.object(restore.subprocess,'Popen') as spawn:
            self.assertIsNone(restore._start_maintenance(self.root,self.operation,3000))
            command.assert_called_once_with(self.root,'restore_maintenance');spawn.assert_not_called()

    def test_web_spawn_does_not_confirm_completion_before_supervised_proxy_readiness(self):
        self.create()
        replies=[{'web_pid':4321,'build_id':'expected-build'}, {'ready':False},
                 {'ready':True,'web_pid':4321,'build_id':'expected-build'}]
        with patch('lifecycle.managed_restore.command',side_effect=replies) as command,patch(
            'lifecycle.managed_restore.bound_journal',return_value=({'operation_id':self.operation},self.bound)),patch(
            'lifecycle.managed_restore.ManagedChild.poll',return_value=None),patch.object(
            restore.state,'_process_start_identity',return_value='987'),patch.object(restore.time,'sleep'):
            child=restore._start_web(self.root,3000,'expected-build')
        self.assertEqual(child.pid,4321)
        self.assertEqual([call.args[1] for call in command.call_args_list],
            ['restore_finish_web','restore_web_readiness','restore_web_readiness'])

    def test_web_readiness_for_another_process_or_build_cannot_complete_restore(self):
        self.create()
        replies=[{'web_pid':4321,'build_id':'expected-build'},
                 {'ready':True,'web_pid':4322,'build_id':'another-build'}]
        with patch('lifecycle.managed_restore.command',side_effect=replies),patch(
            'lifecycle.managed_restore.bound_journal',return_value=({'operation_id':self.operation},self.bound)),patch(
            'lifecycle.managed_restore.ManagedChild.poll',return_value=None),patch.object(
            restore.state,'_process_start_identity',return_value='987'):
            with self.assertRaises(restore.RestoreRecoveryRequired):restore._start_web(self.root,3000,'expected-build')


if __name__=='__main__':unittest.main()
