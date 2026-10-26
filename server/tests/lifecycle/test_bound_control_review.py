"""Canonical UID control review; artifact/OS boundaries have separate real tests."""
from dataclasses import replace
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_control_repair as bound, management_journal as contract, management_store, state
from lifecycle.control.rpc import ControlError
from lifecycle.control import workers
from lifecycle.control.requests import parse_management_request
from tests.lifecycle import test_bound_management_repair as fixtures
from tests.lifecycle.test_management_requests import request_payload


class BoundControlReviewTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.BoundManagementRepairTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.root=fixture.root;self.execution=fixture.execution
        fixture.layout.descriptor.descriptor.installation_id=fixture.source.installation_id
        self.artifact={'bundle_id':self.execution.control_bundle_id,'manifest_sha256':self.execution.control_manifest_sha256,
            'artifact_sha256':'b'*64,'entry_sha256':'c'*64}
        self.enterContext(patch.object(bound,'ROOT',self.root))
        self.enterContext(patch.object(bound,'layout_for_root',return_value=fixture.layout))
        self.enterContext(patch.object(bound,'require_stopped'))
        self.enterContext(patch.object(bound,'require_service_quiescent'))
        self.enterContext(patch.object(bound,'controller_peer'))
        self.enterContext(patch.dict(os.environ,{'INVOCATION_ID':'2'*32}))
        self.enterContext(patch.object(bound,'__file__',str(bound.entry.CONTROL/'rescue-versions'/self.execution.control_bundle_id/'lib/lifecycle/bound_control_repair.py')))
        self.verified=self.enterContext(patch.object(bound,'_artifact',return_value=SimpleNamespace(**self.artifact)))
        self.enterContext(patch.object(bound,'read_management_request',return_value=SimpleNamespace(previous=self.execution)))
        self.enterContext(patch.object(bound.control_reconstruction,'fingerprint',return_value='d'*64))

    def review(self):
        return bound.plan(self.fixture.source.operation_id,self.artifact,
            control_manifest_sha256=self.execution.control_manifest_sha256)

    def test_distinct_control_review_preserves_exact_original_canonical_and_filesystem_state(self):
        paths=state.lifecycle_paths(self.root)
        before={p:p.read_bytes() for p in paths.lifecycle_dir.rglob('*') if p.is_file()}
        value=self.review()
        self.assertEqual(value['roles'],['controller','runtime'])
        self.assertEqual(value['permitted_actions'],['repair_bound_control'])
        self.assertEqual(value['artifact'],self.artifact)
        self.assertEqual(value['selection'],{'schema_version':1,'bundle_id':self.artifact['bundle_id'],
            'manifest_sha256':self.artifact['manifest_sha256']})
        self.assertEqual(before,{p:p.read_bytes() for p in paths.lifecycle_dir.rglob('*') if p.is_file()})
        self.assertEqual(self.fixture.database.read_bytes(),b'preserve parent data')
        self.assertEqual(value,self.review())

    def test_valid_artifact_alone_cannot_choose_an_unbound_control_identity(self):
        self.verified.return_value=SimpleNamespace(**{**self.artifact,'bundle_id':str(uuid.uuid4())})
        with self.assertRaisesRegex(ControlError,'control_reconstruction_identity_changed'):self.review()
        self.assertEqual(state.inspect_state(self.root),self.fixture.claim.state)

    def test_review_refuses_changed_canonical_journal_and_wrong_reader(self):
        journal=self.fixture.source
        verified=self.verified.return_value
        def changed(_):
            from lifecycle.management_journal import ManagementStage
            self.fixture.source=management_store.advance_management_journal(self.root,journal,
                replace(journal,stage=ManagementStage.PREPARED),lease=self.fixture.lease)
            return verified
        self.verified.side_effect=changed
        with self.assertRaises(ControlError):self.review()
        self.verified.side_effect=None
        with self.assertRaisesRegex(ControlError,'control_operation_reader_changed'):
            bound.plan(journal.operation_id,self.artifact,control_manifest_sha256='f'*64)

    def test_root_or_unqualified_runtime_identity_cannot_run_uid_canonical_review(self):
        with patch.object(bound.os,'geteuid',return_value=0),self.assertRaisesRegex(ControlError,'control_worker_forbidden'):
            self.review()
        with patch.object(bound,'layout_for_root',return_value=SimpleNamespace(managed=False)),self.assertRaises(ControlError):
            self.review()

    def test_worker_routes_only_bounded_artifact_data_after_the_service_uid_gate(self):
        operation=self.fixture.source.operation_id
        selected=SimpleNamespace(descriptor=SimpleNamespace(service_uid=__import__('os').geteuid()))
        output=io.StringIO()
        with patch.object(workers,'inspect_installation',return_value=selected), \
                patch.object(workers.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps({'artifact':self.artifact}).encode()))), \
                patch.object(bound,'plan',return_value={'read_only':True}) as plan,redirect_stdout(output):
            self.assertEqual(workers.main(['bound-control-review',operation],bundle=self.root,
                manifest={},manifest_sha256='e'*64),0)
        plan.assert_called_once_with(operation,self.artifact,control_manifest_sha256='e'*64)
        self.assertEqual(json.loads(output.getvalue()),{'read_only':True})
        for payload,uid,code in (({'artifact':self.artifact,'unknown':True},selected.descriptor.service_uid,'control_request_invalid'),
                ({'artifact':self.artifact},0,'control_worker_forbidden')):
            with self.subTest(uid=uid,code=code),patch.object(workers,'inspect_installation',return_value=selected), \
                    patch.object(workers.os,'geteuid',return_value=uid), \
                    patch.object(workers.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
                    patch.object(bound,'plan') as plan,self.assertRaisesRegex(ControlError,code):
                workers.main(['bound-control-review',operation],bundle=self.root,manifest={},manifest_sha256='e'*64)
            plan.assert_not_called()

    def actor(self):
        source=self.review();journal=self.fixture.source
        request=request_payload()
        request.update(installation_id=journal.installation_id,operation_id=journal.operation_id,
            review_id=journal.review_id,expected_generation=journal.generation-1,
            previous=contract.execution_tuple_payload(journal.previous),candidate=contract.execution_tuple_payload(journal.candidate),
            supervisor_invocation_id=journal.supervisor_invocation_id,
            controller_control_manifest_sha256=self.execution.control_manifest_sha256)
        request=parse_management_request(request)
        bound.read_management_request.return_value=request
        self.enterContext(patch.object(bound.recovery_requests.request_contract,'read_management_request',return_value=request))
        scope={'unit':'streamhome-install-'+request.installation_id+'-'+'a'*32+'.scope',
            'invocation':'b'*32,'pid':1234,'start_identity':'5678'}
        grant=bound.prepare({'operation_id':request.operation_id,'artifact':self.artifact,
            'expected_evidence_sha256':source['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
            'issuer_scope':scope,'boot_id':str(uuid.uuid4()),'stop_epoch':'e'*64},
            control_manifest_sha256=self.execution.control_manifest_sha256)
        self.enterContext(patch.object(bound.recovery_requests,'read_grant',return_value=grant))
        self.fixture.lease.release()
        actor=bound.BoundControlController(grant,control_manifest_sha256=self.execution.control_manifest_sha256,checkpoint=lambda:None)
        self.addCleanup(lambda:actor.lease.release() if actor.lease else None)
        return actor

    def test_prepared_control_grant_preserves_original_request_and_carries_no_start_permission(self):
        actor=self.actor()
        # The helper released the fixture lease but did not execute a takeover.
        original=replace(parse_management_request(actor.grant['request']),expected_generation=0)
        bound.read_management_request.return_value=original
        source=self.review()
        scope={'unit':'streamhome-install-'+original.installation_id+'-'+'a'*32+'.scope',
            'invocation':'b'*32,'pid':1234,'start_identity':'5678'}
        payload={'operation_id':original.operation_id,'artifact':self.artifact,
            'expected_evidence_sha256':source['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
            'issuer_scope':scope,'boot_id':str(uuid.uuid4()),'stop_epoch':'e'*64}
        with patch.object(bound.recovery_requests.request_contract,'read_management_request',return_value=original):
            grant=bound.prepare(payload,control_manifest_sha256=self.execution.control_manifest_sha256)
            self.assertEqual(bound.recovery_requests.parse_grant(grant),grant)
            self.assertEqual(grant['schema_version'],5)
            self.assertEqual(grant['request']['previous'],original.payload()['previous'])
            self.assertEqual(grant['request']['candidate'],original.payload()['candidate'])
            self.assertIsNone(grant['request']['backend'])
            self.assertEqual(grant['source_request_sha256'],original.sha256)
            self.assertNotIn('resumption_plan_id',grant);self.assertNotIn('resume_service',grant)
            self.assertEqual(state.inspect_state(self.root),self.fixture.claim.state)
            for changes in ({'action':'repair_bound_environment'},{'resumption_plan_id':str(uuid.uuid4())},
                    {'expires_at_monotonic':True},{'source_request_sha256':'f'*64},
                    {'selection':{**grant['selection'],'schema_version':True}},
                    {'request':{**grant['request'],'expected_generation':0}},
                    {'request':{**grant['request'],'boot_enabled':not grant['request']['boot_enabled']}}):
                value=deepcopy(grant);value.update(changes)
                with self.subTest(changes=changes),self.assertRaises((ControlError,contract.ManagementJournalError)):
                    bound.recovery_requests.parse_grant(value)
            with self.assertRaisesRegex(ControlError,'control_recovery_review_stale'):
                bound.prepare({**payload,'expected_evidence_sha256':'f'*64},
                    control_manifest_sha256=self.execution.control_manifest_sha256)

    def effect(self,actor,*,wrong=False):
        _,path=management_store._journal_path(self.root,self.fixture.source.operation_id)
        intent=bound.recovery._read(path.parent/'control-repair-intent-2.json')
        details={'selection':actor.grant['selection'],'artifact_sha256':self.artifact['artifact_sha256'],
            'filesystem_sha256':actor.grant['filesystem_sha256'],'stage':'complete'}
        return {'schema_version':1,'intent_sha256':'f'*64 if wrong else bound._digest(intent),'effect':details}

    def test_original_owner_rotates_and_waits_for_exact_root_effect_without_accepting_or_completing(self):
        actor=self.actor();original=self.fixture.source
        with patch('lifecycle.control.tickets.read_protected_json',side_effect=lambda path:self.effect(actor)), \
                patch.object(bound.entry,'verify_bundle',return_value=(self.root,{},self.execution.control_manifest_sha256)):
            result=actor.execute()
        current=state.inspect_state(self.root)
        self.assertEqual(current.generation,2)
        self.assertEqual(current.active_operation.operation_id,original.operation_id)
        self.assertEqual(current.active_operation.phase,state.LifecyclePhase.RECOVERY_REQUIRED)
        journal=management_store.inspect_management_journal(self.root,original.operation_id)
        self.assertEqual(journal.previous,original.previous);self.assertEqual(journal.candidate,original.candidate)
        self.assertEqual(journal.acceptance,original.acceptance)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(self.fixture.database.read_bytes(),b'preserve parent data')

    def test_unrelated_root_effect_cannot_publish_repaired_receipt_or_release_parent_authority(self):
        actor=self.actor()
        with patch('lifecycle.control.tickets.read_protected_json',side_effect=lambda path:self.effect(actor,wrong=True)), \
                patch.object(bound.entry,'verify_bundle') as verify,self.assertRaisesRegex(ControlError,'control_reconstruction_review_changed'):
            actor.execute()
        verify.assert_not_called()
        current=state.inspect_state(self.root)
        self.assertEqual(current.generation,2);self.assertIsNotNone(current.active_operation)
        _,path=management_store._journal_path(self.root,self.fixture.source.operation_id)
        self.assertFalse((path.parent/'control-repair-2.json').exists())

    def test_controller_requires_the_exact_service_reader_cgroup_and_os_invocation(self):
        actor=self.actor();grant=actor.grant
        with patch.object(bound.os,'geteuid',return_value=0),patch.object(bound.recovery_requests,'parse_grant') as parse, \
                self.assertRaisesRegex(ControlError,'control_controller_forbidden'):
            bound.BoundControlController({},control_manifest_sha256='f'*64,checkpoint=lambda:None)
        parse.assert_not_called()
        with self.assertRaisesRegex(ControlError,'control_controller_forbidden'):
            bound.BoundControlController(grant,control_manifest_sha256='f'*64,checkpoint=lambda:None)
        with patch.object(bound.controller_peer,'side_effect',ControlError('control_controller_forbidden')),self.assertRaises(ControlError):
            bound.BoundControlController(grant,control_manifest_sha256=self.execution.control_manifest_sha256,checkpoint=lambda:None)
        with patch.dict(os.environ,{'INVOCATION_ID':'0'*32}),self.assertRaises(ControlError):
            bound.BoundControlController(grant,control_manifest_sha256=self.execution.control_manifest_sha256,checkpoint=lambda:None)
        with patch.object(bound,'__file__',str(bound.entry.CONTROL/'versions'/self.execution.control_bundle_id/'lib/lifecycle/bound_control_repair.py')), \
                self.assertRaisesRegex(ControlError,'control_reconstruction_independent_reader_required'):
            bound.BoundControlController(grant,control_manifest_sha256=self.execution.control_manifest_sha256,checkpoint=lambda:None)
        self.assertEqual(state.inspect_state(self.root),self.fixture.claim.state)

    def test_lost_root_grant_refuses_before_original_owner_rotation(self):
        actor=self.actor()
        bound.recovery_requests.read_grant.side_effect=ControlError('control_recovery_permission_expired')
        with self.assertRaisesRegex(ControlError,'control_recovery_permission_expired'):actor.execute()
        self.assertIsNone(actor.lease)
        self.assertEqual(state.inspect_state(self.root),self.fixture.claim.state)

    def test_independent_observer_and_result_bind_exact_current_lease_and_root_effect(self):
        actor=self.actor();observations=[]
        def reply(path):
            self.assertEqual(path.name,'control-repair-effect-2.json')
            payload={'operation_id':self.fixture.source.operation_id,'controller_pid':os.getpid(),
                'controller_invocation_id':actor.invocation}
            value=bound.observe(payload,control_manifest_sha256=self.execution.control_manifest_sha256)
            observations.append(value)
            with self.assertRaisesRegex(ControlError,'control_controller_changed'):
                bound.observe({**payload,'controller_pid':os.getpid()+100000},
                    control_manifest_sha256=self.execution.control_manifest_sha256)
            return self.effect(actor)
        with patch('lifecycle.control.tickets.read_protected_json',side_effect=reply), \
                patch.object(bound.entry,'verify_bundle',return_value=(self.root,{},self.execution.control_manifest_sha256)):
            actor.execute()
        self.assertEqual(observations[0]['stage'],'control_repair_requested')
        self.assertEqual(observations[0]['generation'],2)
        with patch('lifecycle.control.tickets.read_protected_json',return_value=self.effect(actor)), \
                patch.object(bound.entry,'verify_bundle',return_value=(self.root,{},self.execution.control_manifest_sha256)):
            result=bound.result(self.fixture.source.operation_id,control_manifest_sha256=self.execution.control_manifest_sha256)
        self.assertEqual(result['stage'],'control_repaired_recovery_required')
        self.assertTrue(result['completed'])
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)


if __name__=='__main__':unittest.main()
