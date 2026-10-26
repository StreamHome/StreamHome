"""Control producer ordering around actual UID canonical/configuration review."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import execution_state, state
from lifecycle.control import control_maintenance as producer
from lifecycle.control.artifact_stage import PreparedControl
from lifecycle.control.rpc import ControlError
from lifecycle.control.stopped_repair import StoppedRepairHandoff
from tests.lifecycle import test_control_maintenance_review as review_fixture


class ControlMaintenanceProducerTests(unittest.TestCase):
    def setUp(self):
        fixture=review_fixture.ControlMaintenanceReviewTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.identity=fixture.fixture.identity;self.previous=fixture.previous
        self.manifest={'bundle_id':self.previous.control_bundle_id,'support':{'control_maintenance_versions':[1]}}
        candidate=fixture.payload['control_candidate']
        self.prepared=PreparedControl(candidate['control_bundle_id'],candidate['control_manifest_sha256'],'a'*64,'b'*64)
        self.verified=SimpleNamespace(**self.prepared.__dict__)
        self.snapshot={'installation_id':self.identity,'lifecycle':{'active_operation':None,'generation':0},
            'execution':execution_state.read_active_execution(fixture.root).payload(),
            'configuration':fixture.fixture.store.inspect().payload(),
            'active_deployment':fixture.fixture.initial.settings.payload(),'runtime_verified':True}
        self.processes={'initial_service_state':'stopped','supervisor_invocation_id':None,'supervisor':None,'backend':None}
        self.package=Path('/explicit/local/control.deb');self.operation=str(uuid.uuid4());self.events=[]
        self.enterContext(patch.object(producer,'require_root'))
        self.enterContext(patch.object(producer,'descriptor',return_value=SimpleNamespace(
            installation_id=self.identity,status=SimpleNamespace(value='ready'))))
        self.enterContext(patch.object(producer.entry,'verify_bundle',return_value=(Path('/verified/old'),self.manifest,self.previous.control_manifest_sha256)))
        old={'schema_version':1,'bundle_id':self.previous.control_bundle_id,'manifest_sha256':self.previous.control_manifest_sha256}
        self.enterContext(patch.object(producer.entry,'_read',return_value=json.dumps(old).encode()))
        self.enterContext(patch.object(producer.artifacts,'verify_local',return_value=self.verified))
        self.enterContext(patch.object(producer.artifacts,'require_upgrade_support'))
        self.prepare=self.enterContext(patch.object(producer.artifact_stage,'prepare_local',return_value=self.prepared))
        self.enterContext(patch.object(producer.artifact_stage,'require_prepared'))
        self.enterContext(patch.object(producer,'root_stop_epoch',return_value='c'*64))
        self.enterContext(patch.object(producer,'current_scope',return_value={'unit':'fixed-test-scope'}))
        self.enterContext(patch.object(producer,'require_scope'))
        self.enterContext(patch.object(producer,'require_stopped'))
        self.enterContext(patch.object(producer,'_verify_owned_units'))
        self.enterContext(patch.object(producer,'process_snapshot',return_value=self.processes))
        self.enterContext(patch.object(producer,'_boot_enabled',return_value=False))
        self.consent=self.enterContext(patch.object(producer,'_consent',return_value=True))
        for name in ('publish_request','publish_operation_reader','bind_prepared','issue','run_operation'):
            self.enterContext(patch.object(producer,name,side_effect=lambda *args,_name=name,**kwargs:self.events.append(_name)))
        def worker(arguments,**kwargs):
            if arguments==['inspect']:return deepcopy(self.snapshot)
            self.assertEqual(arguments,['prepare-request'])
            return fixture.prepare(kwargs['payload'])
        self.worker=self.enterContext(patch.object(producer,'run_worker',side_effect=worker))

    def maintain(self,*,scoped=True):
        options={'scope_epoch':'c'*64,'operation_id':self.operation,'expected_installation_id':self.identity} if scoped else {}
        with redirect_stdout(io.StringIO()):
            return producer.maintain(self.package,manifest=self.manifest,
                manifest_sha256=self.previous.control_manifest_sha256,**options)

    def test_initial_call_hands_off_before_preparation_consent_or_root_publication(self):
        result=self.maintain(scoped=False)
        self.assertIsInstance(result,StoppedRepairHandoff)
        self.assertEqual(result.control_artifact,self.package)
        self.prepare.assert_not_called();self.consent.assert_not_called();self.assertEqual(self.events,[])

    def test_scoped_approved_review_binds_artifact_and_permission_before_controller_start(self):
        before=self.fixture.fixture.paths.state_path.read_bytes()
        self.assertEqual(self.maintain(),0)
        self.assertEqual(self.events,['publish_request','publish_operation_reader','bind_prepared','issue','run_operation'])
        self.assertEqual(self.fixture.fixture.paths.state_path.read_bytes(),before)
        request=self.consent.call_args.args[0]
        self.assertEqual(request['candidate']['control_bundle_id'],self.prepared.bundle_id)
        self.assertEqual(request['initial_service_state'],'stopped')

    def test_decline_preserves_idle_canonical_state_and_does_not_publish_permission(self):
        self.consent.return_value=False
        self.assertEqual(self.maintain(),0)
        self.assertEqual(self.events,[])
        self.assertIsNone(state.inspect_state(self.fixture.root).active_operation)

    def test_changed_snapshot_after_consent_cannot_start_the_controller(self):
        original=self.worker.side_effect;count=[0]
        def changed(arguments,**kwargs):
            value=original(arguments,**kwargs)
            if arguments==['inspect']:
                count[0]+=1
                if count[0]==2:value['lifecycle']['generation']=1
            return value
        self.worker.side_effect=changed
        with self.assertRaises(ControlError):self.maintain()
        self.assertEqual(self.events,[])

    def test_changed_authenticated_artifact_during_preparation_cannot_reach_consent(self):
        self.prepare.return_value=PreparedControl(self.prepared.bundle_id,self.prepared.manifest_sha256,'f'*64,self.prepared.entry_sha256)
        with self.assertRaises(ControlError):self.maintain()
        self.consent.assert_not_called();self.assertEqual(self.events,[])

    def test_unqualified_reader_cannot_enter_the_maintenance_scope(self):
        self.manifest['support']={}
        with self.assertRaisesRegex(ControlError,'control_maintenance_not_available'):self.maintain(scoped=False)
        self.worker.assert_not_called();self.assertEqual(self.events,[])
