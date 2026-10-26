"""Protected initial control permission is finite, scoped and cannot be renewed."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import control_permission as permission
from lifecycle import management_controller, management_journal
from lifecycle.control.rpc import ControlError


class InitialControlPermissionTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.request={'operation_id':str(uuid.uuid4()),'installation_id':str(uuid.uuid4()),'boot_id':str(uuid.uuid4()),
            'stop_epoch':'a'*64,'controller_control_manifest_sha256':'b'*64,'action':'provision_control',
            'activation_mode':'cutover','interruption_approved':True,'initial_service_state':'stopped'}
        self.owner={'schema_version':1,'pid':os.getpid(),'start_identity':'123','boot_id':self.request['boot_id'],
            'nonce':'c'*64,'status':'active'}
        self.evidence={'owner':self.owner,'scope':{'unit':'streamhome-install-'+self.request['installation_id']+'-'+'d'*32+'.scope',
            'invocation':'e'*32,'pid':os.getpid(),'start_identity':'123'}}
        (self.root/self.request['operation_id']).mkdir()
        self.enterContext(patch.object(permission,'OPERATIONS',self.root))
        self.enterContext(patch.object(permission,'require_root'))
        self.enterContext(patch.object(permission,'descriptor',return_value=SimpleNamespace(
            installation_id=self.request['installation_id'],service_gid=os.getgid())))
        self.enterContext(patch.object(permission,'current_issuer',return_value=self.evidence))
        self.enterContext(patch.object(permission,'read_stop_epoch',return_value=self.request['stop_epoch']))
        self.enterContext(patch.object(permission.Path,'read_text',return_value=self.request['boot_id']))
        self.clock=self.enterContext(patch.object(permission.time,'monotonic',return_value=100))
        self.scope=self.enterContext(patch.object(permission,'require_scope'))
        self.enterContext(patch.object(permission,'read_protected_json',side_effect=lambda path,**kwargs:
            self.owner if path==permission.OWNER else json.loads(path.read_bytes())))
        self.write=self.enterContext(patch.object(permission,'atomic_root_json',side_effect=lambda path,value,**kwargs:
            path.write_bytes(json.dumps(value).encode())))

    @property
    def path(self):return self.root/self.request['operation_id']/'control-permission.json'

    def test_exact_request_can_reuse_its_live_permission_without_extending_or_starting(self):
        value=permission.issue(self.request);original=self.path.read_bytes()
        self.clock.return_value=200
        self.assertEqual(permission.issue(self.request),value)
        self.assertEqual(permission.require_initial(self.request),value)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(self.write.call_count,1)
        self.assertEqual(value['expires_at_monotonic'],1000)

    def test_stale_boot_stop_or_changed_request_cannot_issue_or_use_permission(self):
        for change in ({'boot_id':str(uuid.uuid4())},{'stop_epoch':'f'*64},
                {'interruption_approved':False},{'initial_service_state':{}},{'initial_service_state':'unknown'}):
            with self.subTest(change=change),self.assertRaises(ControlError):permission.issue({**self.request,**change})
        self.write.assert_not_called()
        permission.issue(self.request)
        with self.assertRaises(ControlError):permission.require_initial({**self.request,'extra':'changed request'})

    def test_dead_scope_or_replaced_administrator_fences_initial_permission(self):
        permission.issue(self.request)
        self.scope.side_effect=ControlError('scope stopped')
        with self.assertRaises(ControlError):permission.require_initial(self.request)
        self.scope.side_effect=None
        self.owner={**self.owner,'nonce':'f'*64}
        with patch.object(permission.os,'geteuid',return_value=0),self.assertRaises(ControlError):
            permission.require_initial(self.request)

    def test_expired_nonfinite_or_boolean_deadline_never_renews(self):
        permission.issue(self.request);original=self.path.read_bytes()
        self.clock.return_value=1000
        with self.assertRaises(ControlError):permission.issue(self.request)
        self.assertEqual(self.path.read_bytes(),original)
        self.clock.return_value=200
        for expiry in (True,float('inf'),1900):
            value=json.loads(original);value['expires_at_monotonic']=expiry
            self.path.write_bytes(json.dumps(value).encode())
            with self.subTest(expiry=expiry),self.assertRaises(ControlError):permission.require_initial(self.request)

    def test_foreign_operation_or_control_identity_cannot_borrow_protected_evidence(self):
        permission.issue(self.request)
        original=json.loads(self.path.read_bytes())
        for change in ({'control_manifest_sha256':'f'*64},{'operation_id':str(uuid.uuid4())},
                {'schema_version':True},{'issuer':{**self.evidence,'unexpected':'field'}}):
            value=deepcopy(original);value.update(change);self.path.write_bytes(json.dumps(value).encode())
            with self.subTest(change=change),self.assertRaises(ControlError):permission.require_initial(self.request)

    def test_permission_module_imports_no_canonical_or_application_code(self):
        code="import sys;import lifecycle.control.control_permission;assert not {'lifecycle.state','lifecycle.management_journal','config','application','database'}.intersection(sys.modules)"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_original_controller_rechecks_scope_on_every_checkpoint(self):
        permission.issue(self.request)
        controller=management_controller.ManagementController.__new__(management_controller.ManagementController)
        controller.review=SimpleNamespace(action=management_journal.ManagementAction.PROVISION_CONTROL,
            activation_mode=management_journal.ActivationMode.CUTOVER,initial_service_state='stopped',
            boot_id=self.request['boot_id'],stop_epoch=self.request['stop_epoch'],supervisor=None,
            payload=lambda:self.request)
        controller.cancelled=False;controller.lease=None
        controller.require_preparation_issuer=lambda:None
        with patch.object(management_controller,'stop_epoch',return_value=self.request['stop_epoch']):
            controller.checkpoint()
            self.scope.side_effect=ControlError('root scope stopped')
            with self.assertRaises(ControlError):controller.checkpoint()

    def test_stopped_recovery_requires_its_fresh_retained_scope_grant_without_start_permission(self):
        request={**self.request,'expected_generation':7}
        grant={'schema_version':2,'request':request,'recovery_review_id':str(uuid.uuid4()),
            'source_request_sha256':'f'*64,'expires_at_monotonic':1000,'issuer_scope':self.evidence['scope']}
        directory=self.root/request['operation_id']
        for name in ('recovery-8.json','recovery-review-'+grant['recovery_review_id']+'.json'):
            (directory/name).write_bytes(json.dumps(grant).encode())
        self.assertEqual(permission.require_stopped_recovery(grant),{'issuer':self.evidence})
        with self.assertRaises(ControlError):permission.require_stopped_recovery({**grant,'schema_version':3})
        self.clock.return_value=1000
        with self.assertRaises(ControlError):permission.require_stopped_recovery(grant)
        self.clock.return_value=100
        (directory/'recovery-8.json').write_bytes(b'{}')
        with self.assertRaises(ControlError):permission.require_stopped_recovery(grant)
