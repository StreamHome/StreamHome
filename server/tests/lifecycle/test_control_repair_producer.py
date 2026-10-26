"""Real signed copies/root file effects; explicit UID/OS/capability substitutes."""
from dataclasses import asdict
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import administration, artifact_stage, control_repair as root, entry, stopped_repair, unit_payload
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_control_artifacts as artifact_fixtures


class RootControlRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):artifact_fixtures.ControlArtifactTests.setUpClass()

    @classmethod
    def tearDownClass(cls):artifact_fixtures.ControlArtifactTests.tearDownClass()

    def setUp(self):
        fixture=artifact_fixtures.ControlArtifactTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture
        repair,control,target,selection,_=fixture.reconstruction_fixture()
        self.control=control;self.target=target;self.selection=selection
        self.operation=str(uuid.uuid4());self.installation=str(uuid.uuid4());self.epoch='d'*64
        self.operations=control/'operations';self.operations.mkdir(mode=0o755)
        verified=fixture.verify()
        self.prepared=artifact_stage.PreparedControl(verified.bundle_id,verified.manifest_sha256,verified.artifact_sha256,verified.entry_sha256)
        (control/'selected.json').write_text(json.dumps(selection));(control/'selected.json').chmod(0o644)
        self.bundle=control/'rescue-versions'/selection['bundle_id']
        actual=json.loads((self.bundle/'manifest.json').read_bytes())
        self.manifest=actual
        self.selected=SimpleNamespace(status=SimpleNamespace(value='ready'),installation_id=self.installation,service_gid=os.getgid())
        self.scope={'unit':'streamhome-install-'+self.installation+'-'+'a'*32+'.scope',
            'invocation':'b'*32,'pid':os.getpid(),'start_identity':'123'}
        self.issuer={'owner':{'pid':os.getpid(),'nonce':'a'*64},'scope':self.scope}
        self.snapshot={'operation_id':self.operation,'installation_id':self.installation,'artifact':asdict(self.prepared),
            'controller_selection':selection,'read_only':True,'permitted_actions':['repair_bound_control'],
            'generation':1,'selection':selection,'evidence_sha256':'1'*64,'source_evidence_sha256':'2'*64,
            'source_request_sha256':'3'*64,'filesystem_sha256':repair.fingerprint(target),
            'management_action':'configure_apply','activation_mode':'cutover'}
        self.unit={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        self.grant=None;self.writes=[];self.events=[]
        self.native_kind=None
        for module in (root,administration):
            self.enterContext(patch.object(module,'OPERATIONS',self.operations))
            self.enterContext(patch.object(module,'require_root'))
            self.enterContext(patch.object(module,'descriptor',return_value=self.selected))
            self.enterContext(patch.object(module,'read_protected_json',side_effect=lambda p,**kw:json.loads(p.read_bytes())))
            self.enterContext(patch.object(module,'atomic_root_json',side_effect=self.write))
        self.enterContext(patch.object(administration,'root_directory',side_effect=artifact_stage.root_directory))
        self.enterContext(patch.object(root,'require_stopped'))
        self.enterContext(patch('lifecycle.control.stop._require_empty'))
        self.enterContext(patch.object(root,'root_stop_epoch',return_value=self.epoch))
        self.enterContext(patch.object(root,'current_scope',return_value=self.scope))
        self.enterContext(patch.object(root,'current_issuer',return_value=self.issuer))
        self.enterContext(patch.object(root,'_boot_enabled',return_value=True))
        self.real_root_readers=root._root_readers
        self.enterContext(patch.object(root,'_root_readers'))
        self.enterContext(patch.object(root,'operation_unit',side_effect=lambda op:dict(self.unit)))
        self.systemctl=self.enterContext(patch.object(root,'systemctl',side_effect=self.os_step))
        expected=unit_payload.parse((self.bundle/'lib/lifecycle/control/units.py').read_bytes())
        def units(*,expected: dict,alternate):
            self.assertEqual(expected,self.unit_bytes);self.assertIsNone(alternate)
        self.unit_bytes=expected
        self.enterContext(patch.object(root,'_verify_owned_units',side_effect=units))
        self.reader=self.enterContext(patch.object(root,'run_worker',side_effect=self.worker))
        self.consent=self.enterContext(patch.object(root,'_consent',return_value=True))

    def write(self,path,value,**kwargs):
        self.assertTrue(path.is_relative_to(self.operations))
        path.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
        temporary=path.with_name('.test-'+uuid.uuid4().hex)
        with temporary.open('xb') as output:
            os.fchmod(output.fileno(),kwargs.get('mode',0o644))
            output.write(json.dumps(value).encode());output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
        self.writes.append((path.name,value))
        if path.name.startswith('control-repair-effect-') and value['effect']['stage']=='complete':
            self.unit={'ActiveState':'inactive','MainPID':'0','InvocationID':''}

    def os_step(self,*args,**kwargs):
        self.events.append(args)
        if args[0]=='start':
            self.assertEqual(args,('start',f'streamhome-operation@{self.operation}.service'))
            self.assertEqual(json.loads((self.operations/self.operation/'control-repair-reader.json').read_bytes())['stage'],'active')
            self.unit={'ActiveState':'active','MainPID':'123','InvocationID':'a'*32}
            return ''
        self.assertEqual(args[0],'show')
        return 'LoadState=loaded\nFragmentPath=/etc/systemd/system/streamhome-operation@.service\nUser=streamhome\nDropInPaths=\nNeedDaemonReload=no'

    def worker(self,arguments,*,payload=None,**kwargs):
        self.assertEqual(arguments[1],self.operation)
        action=arguments[0]
        if action=='bound-control-review':
            self.assertTrue(kwargs['control_repair_review'])
            return json.loads(json.dumps(self.snapshot))
        self.assertEqual(kwargs['reader_operation_id'],self.operation);self.assertTrue(kwargs['reader_rescue'])
        if action=='bound-control-prepare':
            self.grant={'schema_version':5,'action':'repair_bound_control','request':{'operation_id':self.operation,
                'installation_id':self.installation,'expected_generation':1,'controller_control_manifest_sha256':self.selection['manifest_sha256'],
                'boot_id':payload['boot_id'],'stop_epoch':payload['stop_epoch']},
                'source_request_sha256':self.snapshot['source_request_sha256'],'source_evidence_sha256':self.snapshot['source_evidence_sha256'],
                'recovery_review_id':payload['recovery_review_id'],'repair_review_sha256':self.snapshot['evidence_sha256'],
                'filesystem_sha256':self.snapshot['filesystem_sha256'],'artifact':asdict(self.prepared),'selection':self.selection,
                'issuer_scope':self.scope,'expires_at_monotonic':root.time.monotonic()+800}
            if self.native_kind is not None:
                from lifecycle.control.control_parent_requests import wrap
                native={'schema_version':4,'review_id':payload['recovery_review_id'],'installation_id':self.installation,
                    'operation_id':self.operation,'operation_type':self.native_kind,'generation':1,'action':'repair_bound_control',
                    'parent_binding_sha256':self.snapshot['source_request_sha256'],
                    'control_manifest_sha256':self.selection['manifest_sha256'],
                    'source_evidence_sha256':self.snapshot['evidence_sha256'],
                    'filesystem_sha256':self.snapshot['filesystem_sha256'],'source_journal_sha256':'4'*64,
                    'source_journal_generation':1,'control_artifact':asdict(self.prepared),'control_selection':self.selection,
                    'issuer_scope':self.scope,'boot_id':payload['boot_id'],'stop_epoch':payload['stop_epoch'],
                    'expires_at_monotonic':root.time.monotonic()+800}
                if self.native_kind=='restore':native['execution_sha256']='1'*64
                else:native.update(original_generation=1,active_record_sha256='1'*64,request_sha256='2'*64,
                    database_checkpoint_sha256='3'*64,previous_execution_sha256='1'*64,candidate_execution_sha256='2'*64,
                    accepted_execution_sha256='1'*64,permitted_execution_sha256=['1'*64],runtime_status='offline',
                    source_lifecycle_phase='recovery_required',runtime_link_source_sha256='4'*64,repair_execution_sha256='1'*64)
                self.grant=wrap(native)
            return self.grant
        if action=='bound-control-observe':
            intent={'schema_version':1,'operation_id':self.operation,'generation':2,'action':'repair_bound_control',
                'grant_sha256':root._sha(self.grant.get('owner_grant',self.grant)),'selection':self.selection,'artifact':asdict(self.prepared),
                'filesystem_sha256':self.snapshot['filesystem_sha256']}
            return {'operation_id':self.operation,'generation':2,'intent':intent,'intent_sha256':root._sha(intent),
                'controller_pid':123,'controller_invocation_id':'a'*32,'stage':'control_repair_requested'}
        self.assertEqual(action,'bound-control-result')
        entry.verify_bundle(self.selection)
        return {'completed':True,'stage':'control_repaired_recovery_required','generation':2,
            'grant_sha256':root._sha(self.grant.get('owner_grant',self.grant)),'selection':self.selection}

    def run_repair(self,*,scoped=True):
        return root.repair(self.operation,self.fixture.output/'streamhome-control-bootstrap.deb',bundle=self.bundle,
            manifest=self.manifest,manifest_sha256=self.selection['manifest_sha256'],
            **({'scope_epoch':self.epoch,'expected_installation_id':self.installation} if scoped else {}))

    def test_scoped_bridge_restores_only_exact_normal_tree_after_independent_uid_proof(self):
        with patch.object(root,'open_console'):
            self.assertEqual(self.run_repair(),0)
        entry.verify_bundle(self.selection);entry.verify_bundle(self.selection,rescue=True)
        retained=list(self.target.parent.glob('.repair-'+self.prepared.bundle_id+'-*'))
        self.assertEqual(len(retained),1)
        self.assertEqual((retained[0]/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        self.assertEqual(json.loads((self.control/'selected.json').read_bytes()),self.selection)
        self.assertEqual(json.loads((self.operations/self.operation/'control-repair-reader.json').read_bytes())['stage'],'complete')
        self.assertEqual([args for args in self.events if args[0]=='start'],[('start',f'streamhome-operation@{self.operation}.service')])
        self.assertFalse(self.fixture.marker.exists())

    def test_outer_handoff_and_declined_review_never_claim_or_restore_or_start(self):
        result=self.run_repair(scoped=False)
        self.assertIsInstance(result,stopped_repair.StoppedRepairHandoff)
        self.assertEqual(result.recovery_action,'repair_bound_control')
        self.reader.assert_not_called();self.consent.assert_not_called()
        self.consent.return_value=False
        self.assertEqual(self.run_repair(),0)
        self.assertEqual(self.writes,[])
        self.assertEqual((self.target/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        self.assertFalse(any(args[0]=='start' for args in self.events))

    def test_reader_or_review_change_before_consent_cannot_publish_or_reconstruct(self):
        self.snapshot['controller_selection']={**self.selection,'manifest_sha256':'f'*64}
        with self.assertRaises(ControlError):self.run_repair()
        self.assertEqual(self.writes,[]);self.consent.assert_not_called()

    def test_original_update_and_restore_capsules_keep_native_permissions_and_root_effects_bound(self):
        for kind in ('update','restore'):
            with self.subTest(kind=kind):
                case=RootControlRepairTests();case.setUp()
                try:
                    case.native_kind=kind
                    case.snapshot['source_evidence_sha256']=case.snapshot['evidence_sha256']
                    with patch.object(root,'open_console'):self.assertEqual(case.run_repair(),0)
                    pointer=json.loads((case.operations/case.operation/'recovery-2.json').read_bytes())
                    self.assertEqual(pointer['schema_version'],6)
                    self.assertEqual(pointer['owner_grant']['operation_type'],kind)
                    if kind=='update':
                        self.assertEqual(json.loads((case.operations/case.operation/'update-recovery-2.json').read_bytes()),
                            pointer['owner_grant'])
                    else:self.assertFalse((case.operations/case.operation/'update-recovery-2.json').exists())
                    self.assertEqual([a for a in case.events if a[0]=='start'],
                        [('start',f'streamhome-operation@{case.operation}.service')])
                finally:case.doCleanups()

    def test_unexplained_existing_update_permission_cannot_be_replaced_or_start_any_actor(self):
        self.native_kind='update';self.snapshot['source_evidence_sha256']=self.snapshot['evidence_sha256']
        directory=self.operations/self.operation;directory.mkdir(mode=0o755)
        pointer=directory/'update-recovery-2.json';pointer.write_text('{}');pointer.chmod(0o640)
        with self.assertRaises(ControlError):self.run_repair()
        self.assertEqual(pointer.read_text(),'{}')
        self.assertFalse(any(name.startswith('recovery-') or name.startswith('update-') for name,value in self.writes))
        self.assertFalse(any(args[0]=='start' for args in self.events))

    def test_unadvertised_capability_or_unowned_unit_definition_refuses_before_any_start(self):
        for support in ({},{'bound_control_repair_versions':[True],'maintenance_actions':['repair_bound_control']}):
            with self.subTest(support=support),self.assertRaisesRegex(ControlError,'control_bound_control_repair_unavailable'):
                root.repair(self.operation,self.fixture.output/'streamhome-control-bootstrap.deb',bundle=self.bundle,
                    manifest={**self.manifest,'support':support},manifest_sha256=self.selection['manifest_sha256'])
        self.systemctl.side_effect=lambda *args,**kwargs:'LoadState=loaded\nFragmentPath=/unowned.service\nUser=root\nDropInPaths=\nNeedDaemonReload=no'
        with self.assertRaisesRegex(ControlError,'control_stop_unit_unqualified'):self.run_repair()
        self.assertEqual(self.writes,[]);self.reader.assert_not_called()

    def test_fixed_root_normal_readers_block_replacement_but_same_operation_rescue_processes_do_not(self):
        prefix=b'/usr/bin/python3\0-I\0/usr/local/libexec/streamhome/provision\0'
        class Process:
            def __init__(self,pid,arguments,uid=0):self.name=str(pid);self.arguments=arguments;self.uid=uid
            def stat(self):return SimpleNamespace(st_uid=self.uid)
            def __truediv__(self,name):
                return SimpleNamespace(read_bytes=lambda:b'1 (root) S 0',open=lambda *args:io.BytesIO(self.arguments),
                    readlink=lambda:Path('/usr/bin/systemd-run'))
        safe=[Process(101,prefix+b'--repair-control\0--operation\0'+self.operation.encode()+b'\0'),
            Process(104,prefix+b'--cli\0repair\0--operation='+self.operation.encode()+b'\0--control-artifact=/local/control.deb\0'),
            Process(105,b'/usr/bin/systemd-run\0--scope\0--quiet\0--collect\0--slice=system.slice\0--unit=test\0'+
                prefix+b'--repair-control-scoped\0'+self.installation.encode()+b'\0'+self.operation.encode()+b'\0'+self.epoch.encode()+b'\0'),
            Process(102,prefix+b'--operation\0'+self.operation.encode()+b'\0',uid=998)]
        # Restore the actual scanner; its only input is bounded synthetic /proc.
        with patch.object(root,'Path',return_value=SimpleNamespace(iterdir=lambda:safe)), \
                patch.object(root.os,'getpid',return_value=100):
            self.real_root_readers(self.operation)
        unsafe=Process(103,prefix+b'--cli\0logs\0--follow\0')
        with patch.object(root,'Path',return_value=SimpleNamespace(iterdir=lambda:safe+[unsafe])), \
                patch.object(root.os,'getpid',return_value=100),self.assertRaisesRegex(ControlError,'control_root_reader_busy'):
            self.real_root_readers(self.operation)


class ControlRepairConsentTests(unittest.TestCase):
    def setUp(self):
        self.directory=self.enterContext(tempfile.TemporaryDirectory())
        self.path=Path(self.directory)/'request.json'
        self.snapshot={'operation_id':str(uuid.uuid4())}
        self.prepared=artifact_stage.PreparedControl(str(uuid.uuid4()),'a'*64,'b'*64,'c'*64)
        self.issuer={'scope':'test-only'}
        review={'schema_version':1,'review':self.snapshot,'prepared':asdict(self.prepared),
            'issuer_sha256':root._sha(self.issuer),'boot_enabled':False}
        self.digest=root._sha(review)
        self.real_consent=root._consent

    def consent(self,live=lambda:None):
        with redirect_stdout(io.StringIO()):
            return self.real_consent(self.snapshot,self.prepared,self.issuer,False,self.path,live)

    def request(self,**changes):
        value={'schema_version':1,'expected_review_sha256':self.digest,'repair_bound_control':True}
        value.update(changes);self.path.write_text(json.dumps(value))

    def test_explicit_approval_and_decline_bind_the_exact_published_review(self):
        for approved in (True,False):
            with self.subTest(approved=approved):
                self.request(repair_bound_control=approved)
                self.assertIs(self.consent(),approved)

    def test_stale_review_and_non_boolean_or_extra_fields_are_rejected(self):
        for changes,code in (({'expected_review_sha256':'f'*64},'control_reconstruction_review_changed'),
                ({'repair_bound_control':1},'control_request_invalid'),
                ({'schema_version':True},'control_request_invalid'),
                ({'start_service':True},'control_request_invalid')):
            with self.subTest(changes=changes),self.assertRaisesRegex(ControlError,code):
                self.request(**changes);self.consent()

    def test_revoked_scope_is_checked_before_existing_approval_is_read(self):
        self.request()
        def revoked():raise ControlError('control_operator_stopped')
        with self.assertRaisesRegex(ControlError,'control_operator_stopped'):
            self.consent(revoked)

    def test_missing_approval_expires_without_waiting_or_creating_a_request(self):
        with patch.object(root.time,'monotonic',side_effect=[100,1001]), \
                patch.object(root.time,'sleep') as sleep, \
                self.assertRaisesRegex(ControlError,'control_reconstruction_review_timeout'):
            self.consent()
        sleep.assert_not_called();self.assertFalse(self.path.exists())

    def test_symlink_cannot_supply_approval(self):
        other=Path(self.directory)/'other.json'
        self.request();self.path.rename(other);self.path.symlink_to(other)
        with self.assertRaises(OSError):self.consent()


if __name__=='__main__':unittest.main()
