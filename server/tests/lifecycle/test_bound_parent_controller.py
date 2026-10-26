"""Original parent repair controller with actual custody, cache and SQLite data."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_parent_repair as repair, bound_runtime_repair as artifacts, restore_controller as restore, state
from lifecycle.control import parent_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_bound_parent_repair as fixtures


class BoundParentEnvironmentControllerTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.BoundParentRepairPlanTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.operation=self.fixture.operation
        self.enterContext(patch('lifecycle.control.processes.controller_peer'))
        self.actor={'ActiveState':'active','MainPID':str(os.getpid()),'InvocationID':'a'*32}
        self.enterContext(patch('lifecycle.control.administration.operation_unit',side_effect=lambda op:dict(self.actor)))
        self.enterContext(patch.dict(os.environ,INVOCATION_ID='a'*32))
        self.enterContext(patch.object(parent_grants,'require_scope'))
        self.enterContext(patch.object(parent_grants,'read_stop_epoch',return_value='b'*64))
        self.fast=self.enterContext(patch.object(parent_grants,'require_live_identity'))
        self.grants={}
        self.enterContext(patch.object(parent_grants,'read_grant',side_effect=lambda op,generation,**kwargs:self.grants[generation]))

    def issue(self):
        current=self.fixture.plan()
        scope={'unit':'streamhome-install-'+self.fixture.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        grant=repair.prepare(self.root,{'operation_id':self.operation,
            'expected_evidence_sha256':current['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
            'issuer_scope':scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':'b'*64},
            control_manifest_sha256=self.fixture.bound.execution.control_manifest_sha256)
        self.grants[grant['generation']+1]=grant
        return grant

    def controller(self):return repair.BoundParentEnvironmentController(self.root,self.operation,
        control_manifest_sha256=self.fixture.bound.execution.control_manifest_sha256)

    def result(self):return repair.result(self.root,self.operation,
        control_manifest_sha256=self.fixture.bound.execution.control_manifest_sha256)

    def test_independent_repair_imports_no_application_or_third_party_environment(self):
        code="import sys;import lifecycle.bound_parent_repair;assert not {'config','database','application','fastapi','sqlmodel'}.intersection(sys.modules)"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unaccepted_parent_is_reconstructed_exactly_but_stays_stopped_and_unresolved(self):
        before=self.fixture.database.read_bytes();grant=self.issue()
        actor=self.controller();self.assertEqual(actor.execute(),0)
        authority=state.inspect_state(self.root)
        self.assertEqual(authority.generation,3);self.assertIsNotNone(authority.active_operation)
        self.assertEqual(authority.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(self.fixture.database.read_bytes(),before)
        self.assertEqual(self.result(),{'completed':True,'generation':3,'stage':'environment_repaired_recovery_required'})
        self.assertEqual((self.fixture.fixture.trees['python']/'bin/program').read_bytes(),b'exact immutable bytes')
        journal=restore.inspect_restore_journal(self.root,self.operation)
        self.assertFalse(journal['accepted']);self.assertEqual(journal['managed_execution'],self.fixture.bound.payload())

    def test_actual_accepted_parent_and_database_survive_controller_reconstruction(self):
        self.fixture.accept_parent();before=self.fixture.database.read_bytes();self.issue()
        self.assertEqual(self.controller().execute(),0)
        self.assertEqual(self.fixture.database.read_bytes(),before)
        self.assertTrue(restore.inspect_restore_journal(self.root,self.operation)['accepted'])
        self.assertTrue(self.result()['completed'])

    def test_canceled_partial_repair_keeps_evidence_and_fresh_explicit_retry_ignores_old_expired_issuer(self):
        self.fixture.accept_parent();database=self.fixture.database.read_bytes();old=self.issue()
        actor=self.controller();original=artifacts.reconstruct
        def interrupted(*args,**kwargs):
            checkpoint=kwargs['checkpoint'];receipt=kwargs['receipt']
            def cancel():
                checkpoint()
                if receipt.exists() and json.loads(receipt.read_text())['trees'].get('python',{}).get('stage')=='reconstructing':
                    raise ControlError('control_operator_stopped')
            return original(*args,**{**kwargs,'checkpoint':cancel})
        with patch.object(artifacts,'reconstruct',side_effect=interrupted):
            with self.assertRaisesRegex(ControlError,'control_operator_stopped'):actor.execute()
        authority=state.inspect_state(self.root)
        self.assertEqual(authority.generation,3);self.assertEqual(authority.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(self.fixture.database.read_bytes(),database)
        self.assertTrue(restore.inspect_restore_journal(self.root,self.operation)['accepted'])
        with self.assertRaises(ControlError):self.result()
        with patch.object(parent_grants.time,'monotonic',return_value=old['expires_at_monotonic']+1):
            fresh=self.issue()
            self.actor['InvocationID']='b'*32;os.environ['INVOCATION_ID']='b'*32
            self.assertEqual(self.controller().execute(),0)
        self.assertEqual(fresh['generation'],3)
        self.assertEqual(self.result()['generation'],4)
        self.assertEqual(self.fixture.database.read_bytes(),database)
        self.assertTrue(restore.inspect_restore_journal(self.root,self.operation)['accepted'])


if __name__=='__main__':unittest.main()
