"""Speculative pre-transfer observations never consume OS start permission."""
import json
import os
from unittest.mock import patch
import unittest
import uuid

from lifecycle import state
from lifecycle.control import parent_grants, parent_recover, resumption_dispatch, resumption_store
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_parent_grants as fixtures


class ParentResumptionWaitTests(unittest.TestCase):
    def test_partial_canonical_transfer_waits_without_dispatch_until_independent_proof(self):
        fixture=fixtures.ParentGrantTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        grant={**fixture.prepare(),'schema_version':2,'resumption_plan_id':str(uuid.uuid4())}
        directory=fixture.root/'protected-test-operations'
        def root_directory(path,**kwargs):
            path.mkdir(parents=True,exist_ok=True)
            return os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        def atomic(path,value,**kwargs):path.write_text(json.dumps(value))
        unit={'ActiveState':'active','MainPID':'4321','InvocationID':'a'*32}
        inactive={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        before=state.inspect_state(fixture.root)
        with patch.object(parent_recover,'require_root'),patch.object(parent_recover,'descriptor',
            return_value=type('Descriptor',(),{'installation_id':grant['installation_id'],'service_gid':os.getgid()})()),patch.object(
            parent_grants,'ROOT',directory),patch.object(parent_recover,'root_directory',side_effect=root_directory),patch.object(
            parent_recover,'atomic_root_json',side_effect=atomic),patch(
            'lifecycle.control.tickets.read_protected_json',side_effect=lambda path:json.loads(path.read_text())),patch(
            'lifecycle.control.stop._require_empty'),patch.object(parent_recover,'operation_unit',side_effect=[inactive,unit,unit,unit,inactive]),patch.object(
            parent_recover,'systemctl') as systemctl,patch.object(parent_recover.time,'sleep'),patch.object(
            parent_recover,'run_worker',return_value={'completed':True}),patch.object(resumption_store,'live_state',
            return_value={'plan':{'plan_id':grant['resumption_plan_id']},'receipt':None}),patch.object(
            resumption_dispatch,'_observe',side_effect=[ControlError('control_worker_failed'),
                ControlError('control_controller_changed'),{'independently_verified':True}]) as observe,patch.object(
            resumption_dispatch,'start',return_value=True) as start:
            parent_recover._run(grant)
        self.assertEqual(observe.call_count,3)
        start.assert_called_once_with(grant,control_manifest_sha256=grant['control_manifest_sha256'])
        systemctl.assert_called_once_with('start','streamhome-operation@'+grant['operation_id']+'.service',timeout=30)
        self.assertEqual(state.inspect_state(fixture.root),before)
    def test_parent_repair_root_bridge_starts_only_fixed_operation_and_observes_repair_result(self):
        fixture=fixtures.ParentGrantTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        grant={**fixture.prepare(),'schema_version':3,'action':'repair_bound_environment',
            'filesystem_sha256':'d'*64,'source_journal_sha256':'e'*64,'source_journal_generation':2}
        grant.pop('canonical_evidence_identity')
        directory=fixture.root/'protected-repair-operations'
        def mkdir(path,**kwargs):
            path.mkdir(parents=True,exist_ok=True)
            return os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        unit={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        with patch.object(parent_recover,'require_root'),patch.object(parent_recover,'descriptor',
            return_value=type('Descriptor',(),{'installation_id':grant['installation_id'],'service_gid':os.getgid()})()),patch.object(
            parent_grants,'ROOT',directory),patch.object(parent_recover,'root_directory',side_effect=mkdir),patch.object(
            parent_recover,'atomic_root_json',side_effect=lambda path,value,**kw:path.write_text(json.dumps(value))),patch(
            'lifecycle.control.tickets.read_protected_json',side_effect=lambda path:json.loads(path.read_text())),patch(
            'lifecycle.control.stop._require_empty'),patch.object(parent_recover,'operation_unit',return_value=unit),patch.object(
            parent_recover,'systemctl') as systemctl,patch.object(parent_recover,'run_worker',
            return_value={'completed':True,'stage':'environment_repaired_recovery_required'}) as worker,patch.object(
            resumption_dispatch,'start') as runtime_start:
            parent_recover._run(grant)
        systemctl.assert_called_once_with('start','streamhome-operation@'+grant['operation_id']+'.service',timeout=30)
        runtime_start.assert_not_called();worker.assert_called_once_with(['bound-repair-result',grant['operation_id']],timeout=120)



if __name__=='__main__':unittest.main()
