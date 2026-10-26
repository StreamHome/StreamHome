"""Protected reader pins preserve code identity independently of global selection."""
import hashlib
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import administration, entry, privileged, recover, control_activation
from lifecycle.control.rpc import ControlError


class ControlReaderPinTests(unittest.TestCase):
    def test_public_exact_control_repair_selects_physical_rescue_before_cli_import(self):
        operation=str(uuid.uuid4());bundle=Path('/trusted/physical-rescue')
        with patch.object(entry.sys,'argv',['provision','--cli','repair','--operation',operation,
                '--control-artifact','/local/control.deb']), \
                patch.object(entry,'load_bundle',return_value=(bundle,{},'a'*64)) as load, \
                patch.object(privileged,'dispatch',return_value=0) as dispatch:
            self.assertEqual(entry.main(),0)
        load.assert_called_once_with(rescue=True,operation_id=operation,require_operation_pin=False,
            operation_rescue=False,bound_control_repair=True)
        dispatch.assert_called_once_with(['--repair-control','--operation',operation,'--artifact','/local/control.deb'],
            bundle=bundle,manifest={},manifest_sha256='a'*64,rescue=True)
        with patch.object(entry.sys,'argv',['provision','--cli','repair','--operation='+operation,
                '--control-artifact=/local/control.deb','--request=/local/approval.json']), \
                patch.object(entry,'load_bundle',return_value=(bundle,{},'a'*64)), \
                patch.object(privileged,'dispatch',return_value=0) as dispatch:
            self.assertEqual(entry.main(),0)
        self.assertEqual(dispatch.call_args.args[0],['--repair-control','--operation',operation,
            '--artifact','/local/control.deb','--request','/local/approval.json'])
    def test_exact_control_outer_and_scoped_entries_load_only_operation_bound_physical_rescue(self):
        operation=str(uuid.uuid4());installation=str(uuid.uuid4());bundle=Path('/trusted/physical-rescue')
        for arguments in (['--repair-control','--operation',operation,'--artifact','/local/control.deb'],
                ['--repair-control-scoped',installation,operation,'a'*64,'--artifact','/local/control.deb']):
            with self.subTest(mode=arguments[0]),patch.object(entry.sys,'argv',['provision',*arguments]), \
                    patch.object(entry,'load_bundle',return_value=(bundle,{},'b'*64)) as load, \
                    patch.object(privileged,'dispatch',return_value=0) as dispatch:
                self.assertEqual(entry.main(),0)
            load.assert_called_once_with(rescue=True,operation_id=operation,require_operation_pin=False,
                operation_rescue=False,bound_control_repair=True)
            dispatch.assert_called_once_with(arguments,bundle=bundle,manifest={},manifest_sha256='b'*64,rescue=True)
            with patch.object(entry.sys,'argv',['provision','--rescue',*arguments]), \
                    patch.object(entry,'load_bundle') as load,self.assertRaises(entry.EntryError):entry.main()
            load.assert_not_called()

    def test_only_root_bound_active_actor_role_switches_fixed_operation_entry_to_physical_rescue(self):
        operation=str(uuid.uuid4());bundle=Path('/trusted/physical-rescue')
        with patch.object(entry.sys,'argv',['provision','--operation',operation]), \
                patch.object(entry,'control_repair_role',return_value=True) as role, \
                patch.object(entry,'load_bundle',return_value=(bundle,{},'a'*64)) as load, \
                patch.object(privileged,'dispatch',return_value=0) as dispatch:
            self.assertEqual(entry.main(),0)
        role.assert_called_once_with(operation)
        load.assert_called_once_with(rescue=True,operation_id=operation,require_operation_pin=True,
            operation_rescue=False,bound_control_repair=True)
        dispatch.assert_called_once_with(['--operation',operation],bundle=bundle,manifest={},manifest_sha256='a'*64,rescue=True)

    def test_exact_control_dispatch_releases_lock_before_scoped_handoff(self):
        from lifecycle.control import control_repair,stopped_repair
        events=[];installation=str(uuid.uuid4());operation=str(uuid.uuid4())
        handoff=stopped_repair.StoppedRepairHandoff(installation,operation,'a'*64,None,
            recovery_action='repair_bound_control',control_artifact=Path('/local/control.deb'))
        @contextmanager
        def lock():
            events.append('lock')
            try:yield
            finally:events.append('unlock')
        with patch.object(privileged,'require_root'),patch('lifecycle.control.provisioning.administrative_lock',side_effect=lock), \
                patch.object(control_repair,'repair',return_value=handoff) as repair, \
                patch.object(stopped_repair,'run_scoped',side_effect=lambda value:(events.append('scope'),0)[1]):
            self.assertEqual(privileged.dispatch(['--repair-control','--operation',operation,'--artifact','/local/control.deb'],
                bundle=Path('/trusted/rescue'),manifest={},manifest_sha256='b'*64,rescue=True),0)
        self.assertEqual(events,['lock','unlock','scope'])
        repair.assert_called_once_with(operation,Path('/local/control.deb'),bundle=Path('/trusted/rescue'),
            manifest={},manifest_sha256='b'*64,request_path=None)

    def test_exact_control_outer_and_scoped_dispatch_require_root_before_producer(self):
        from lifecycle.control import control_repair
        for arguments in (['--repair-control','--operation',str(uuid.uuid4()),'--artifact','/local/control.deb'],
                ['--repair-control-scoped',str(uuid.uuid4()),str(uuid.uuid4()),'a'*64,'--artifact','/local/control.deb']):
            with self.subTest(mode=arguments[0]),patch.object(privileged.os,'geteuid',return_value=1000), \
                    patch.object(control_repair,'repair') as repair,redirect_stdout(io.StringIO()), \
                    patch.object(privileged.sys,'stderr',io.StringIO()):
                self.assertEqual(privileged.dispatch(arguments,bundle=Path('/trusted/rescue'),manifest={},
                    manifest_sha256='b'*64,rescue=True),2)
            repair.assert_not_called()

    def test_exact_control_scoped_dispatch_preserves_installation_operation_epoch_and_artifact(self):
        from lifecycle.control import control_repair
        installation=str(uuid.uuid4());operation=str(uuid.uuid4())
        with patch.object(privileged,'require_root'),patch('lifecycle.control.provisioning.administrative_lock'), \
                patch.object(control_repair,'repair',return_value=0) as repair:
            self.assertEqual(privileged.dispatch(['--repair-control-scoped',installation,operation,'a'*64,
                '--artifact','/local/control.deb','--request','/local/approval.json'],bundle=Path('/trusted/rescue'),
                manifest={},manifest_sha256='b'*64,rescue=True),0)
        repair.assert_called_once_with(operation,Path('/local/control.deb'),bundle=Path('/trusted/rescue'),manifest={},
            manifest_sha256='b'*64,request_path=Path('/local/approval.json'),scope_epoch='a'*64,
            expected_installation_id=installation)

    def test_read_only_initial_control_review_uses_selected_physical_rescue_without_publishing_pin(self):
        operation=str(uuid.uuid4());bundle=Path('/trusted/physical-rescue')
        with patch.object(entry.sys,'argv',['provision','--control-repair-review-worker',operation]), \
                patch.object(entry,'load_bundle',return_value=(bundle,{},'a'*64)) as load, \
                patch.object(privileged,'dispatch',return_value=0) as dispatch:
            self.assertEqual(entry.main(),0)
        load.assert_called_once_with(rescue=True,operation_id=operation,require_operation_pin=False,
            operation_rescue=False,bound_control_repair=True)
        dispatch.assert_called_once_with(['--worker','bound-control-review',operation],bundle=bundle,
            manifest={},manifest_sha256='a'*64,rescue=True)
        selected=SimpleNamespace(service_uid=998,service_gid=998)
        with patch.object(privileged,'require_root'),patch.object(privileged,'descriptor',return_value=selected), \
                patch.object(privileged,'_RESCUE_MODE',True),patch.object(privileged.subprocess,'run',
                    return_value=SimpleNamespace(returncode=0,stdout=b'{}',stderr=b'')) as run:
            self.assertEqual(privileged.run_worker(['bound-control-review',operation],control_repair_review=True),{})
        self.assertEqual(run.call_args.args[0],['/usr/bin/python3','-I','/usr/local/libexec/streamhome/provision',
            '--control-repair-review-worker',operation])
        self.assertEqual(run.call_args.kwargs['user'],998)
        self.assertEqual(run.call_args.kwargs['extra_groups'],[])
        for arguments in (['bound-control-prepare',operation],['inspect'],['bound-control-review','../bad']):
            with self.subTest(arguments=arguments),patch.object(privileged,'require_root'), \
                    patch.object(privileged,'descriptor',return_value=selected),patch.object(privileged.subprocess,'run') as run, \
                    self.assertRaises(ControlError):privileged.run_worker(arguments,control_repair_review=True)
            run.assert_not_called()

    def test_maintenance_dispatch_releases_administrator_lock_before_scope_handoff(self):
        from lifecycle.control import control_maintenance,stopped_repair
        events=[];installation=str(uuid.uuid4());operation=str(uuid.uuid4())
        handoff=stopped_repair.StoppedRepairHandoff(installation,operation,'a'*64,None,control_artifact=Path('/local/control.deb'))
        @contextmanager
        def lock():
            events.append('lock')
            try:yield
            finally:events.append('unlock')
        with patch.object(privileged,'require_root'),patch('lifecycle.control.provisioning.administrative_lock',side_effect=lock), \
                patch.object(control_maintenance,'maintain',side_effect=lambda *args,**kwargs:(events.append('review'),handoff)[1]), \
                patch.object(stopped_repair,'run_scoped',side_effect=lambda value:(events.append('scope'),0)[1]):
            self.assertEqual(privileged.dispatch(['--maintain-control','--artifact','/local/control.deb'],bundle=Path('/trusted'),
                manifest={},manifest_sha256='b'*64,rescue=False),0)
        self.assertEqual(events,['lock','review','unlock','scope'])

    def test_maintenance_dispatch_requires_root_before_reading_local_package(self):
        from lifecycle.control import control_maintenance
        with patch.object(privileged.os,'geteuid',return_value=1000),patch.object(control_maintenance,'maintain') as maintain, \
                redirect_stdout(io.StringIO()),patch.object(privileged.sys,'stderr',io.StringIO()):
            self.assertEqual(privileged.dispatch(['--maintain-control','--artifact','/local/control.deb'],bundle=Path('/trusted'),
                manifest={},manifest_sha256='b'*64,rescue=False),2)
        maintain.assert_not_called()

    def test_entry_routes_pinned_worker_to_only_its_operation_reader(self):
        operation=str(uuid.uuid4());bundle=Path('/verified/old')
        with patch.object(entry.sys,'argv',['provision','--operation-worker',operation,'operation-result',operation]), \
                patch.object(entry,'load_bundle',return_value=(bundle,{},'a'*64)) as load, \
                patch.object(privileged,'dispatch',return_value=0) as dispatch:
            self.assertEqual(entry.main(),0)
        load.assert_called_once_with(rescue=False,operation_id=operation,require_operation_pin=True,operation_rescue=False,bound_control_repair=False)
        dispatch.assert_called_once_with(['--worker','operation-result',operation],bundle=bundle,
            manifest={},manifest_sha256='a'*64,rescue=False)

    def test_explicit_exact_rescue_worker_cannot_load_an_unpinned_or_arbitrary_action(self):
        operation=str(uuid.uuid4());bundle=Path('/verified/physical-rescue')
        with patch.object(entry.sys,'argv',['provision','--operation-rescue-worker',operation,'bound-control-review',operation]), \
                patch.object(entry,'load_bundle',return_value=(bundle,{},'a'*64)) as load, \
                patch.object(privileged,'dispatch',return_value=0) as dispatch:
            self.assertEqual(entry.main(),0)
        load.assert_called_once_with(rescue=True,operation_id=operation,require_operation_pin=True,operation_rescue=True,bound_control_repair=False)
        dispatch.assert_called_once_with(['--worker','bound-control-review',operation],bundle=bundle,
            manifest={},manifest_sha256='a'*64,rescue=True)
        for prefix,action in (([], 'inspect'),([], 'prepare-request'),(['--rescue'],'bound-control-review')):
            with self.subTest(prefix=prefix,action=action),patch.object(entry.sys,'argv',
                    ['provision',*prefix,'--operation-rescue-worker',operation,action,operation]), \
                    patch.object(entry,'load_bundle') as load,self.assertRaises(entry.EntryError):entry.main()
            load.assert_not_called()

    def test_exact_rescue_worker_still_drops_uid_and_never_uses_global_rescue_selection(self):
        operation=str(uuid.uuid4());selected=SimpleNamespace(service_uid=998,service_gid=998)
        with patch.object(privileged,'require_root'),patch.object(privileged,'descriptor',return_value=selected), \
                patch.object(privileged,'_RESCUE_MODE',True),patch.object(privileged.subprocess,'run',
                    return_value=SimpleNamespace(returncode=0,stdout=b'{}',stderr=b'')) as run:
            self.assertEqual(privileged.run_worker(['bound-control-review',operation],reader_operation_id=operation,reader_rescue=True),{})
        self.assertEqual(run.call_args.args[0],['/usr/bin/python3','-I','/usr/local/libexec/streamhome/provision',
            '--operation-rescue-worker',operation,'bound-control-review',operation])
        self.assertEqual(run.call_args.kwargs['user'],998)
        self.assertEqual(run.call_args.kwargs['group'],998)
        self.assertEqual(run.call_args.kwargs['extra_groups'],[])
        for reader,action in ((None,'bound-control-review'),(operation,'inspect'),(operation,'prepare-request')):
            with self.subTest(reader=reader,action=action),patch.object(privileged,'require_root'), \
                    patch.object(privileged,'descriptor',return_value=selected),patch.object(privileged.subprocess,'run') as run, \
                    self.assertRaises(ControlError):privileged.run_worker([action],reader_operation_id=reader,reader_rescue=True)
            run.assert_not_called()

    def test_pinned_worker_drops_uid_before_loading_reader(self):
        operation=str(uuid.uuid4())
        selected=SimpleNamespace(service_uid=998,service_gid=998)
        with patch.object(privileged,'require_root'),patch.object(privileged,'descriptor',return_value=selected), \
                patch.object(privileged,'_RESCUE_MODE',True), \
                patch.object(privileged.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'{}',stderr=b'')) as run:
            self.assertEqual(privileged.run_worker(['operation-result',operation],reader_operation_id=operation),{})
        self.assertEqual(run.call_args.args[0],['/usr/bin/python3','-I','/usr/local/libexec/streamhome/provision',
            '--operation-worker',operation,'operation-result',operation])
        self.assertEqual(run.call_args.kwargs['user'],998)
        self.assertEqual(run.call_args.kwargs['group'],998)
        self.assertEqual(run.call_args.kwargs['extra_groups'],[])

    def test_invalid_reader_operation_cannot_spawn_a_process(self):
        with patch.object(privileged,'require_root'),patch.object(privileged,'descriptor'), \
                patch.object(privileged.subprocess,'run') as run:
            for operation in ('../escape','0'*32,str(uuid.UUID(int=0)),None):
                if operation is None:continue  # None deliberately selects the ordinary reader.
                with self.subTest(operation=operation),self.assertRaises(ControlError):
                    privileged.run_worker(['inspect'],reader_operation_id=operation)
            run.assert_not_called()

    def test_root_recovery_review_uses_operation_pin_after_global_reader_changed(self):
        operation=str(uuid.uuid4())
        snapshot={'operation_id':operation,'initial_service_state':'running','activation_mode':'cutover',
            'permitted_actions':['continue_recovery']}
        pin={'schema_version':1,'bundle_id':str(uuid.uuid4()),'manifest_sha256':'a'*64}
        with patch.object(recover,'require_root'),patch.object(entry,'operation_selection',return_value=pin), \
                patch.object(recover,'operation_unit',return_value={'ActiveState':'inactive','MainPID':'0'}), \
                patch('lifecycle.control.stop._require_empty'),patch.object(recover,'root_stop_epoch',return_value='b'*64), \
                patch.object(recover,'run_worker',return_value=snapshot) as reader, \
                patch.object(privileged,'unit_snapshot',return_value={'ActiveState':'inactive','MainPID':'0'}), \
                patch.object(recover,'_preserved_processes',return_value={}),redirect_stdout(io.StringIO()):
            self.assertEqual(recover.recover(operation,request_path=None,control_manifest_sha256='f'*64,review_only=True),0)
        reader.assert_called_once_with(['recovery-review',operation],timeout=120,reader_operation_id=operation)

    def test_control_recovery_with_old_supervisor_alive_requires_an_operator_stop_before_transfer(self):
        operation=str(uuid.uuid4())
        snapshot={'operation_id':operation,'initial_service_state':'running','activation_mode':'cutover',
            'management_action':'provision_control','permitted_actions':['continue_recovery']}
        with patch.object(recover,'require_root'),patch.object(entry,'operation_selection',return_value=None), \
                patch.object(recover,'operation_unit',return_value={'ActiveState':'inactive','MainPID':'0'}), \
                patch('lifecycle.control.stop._require_empty'),patch.object(recover,'root_stop_epoch',return_value='b'*64), \
                patch.object(recover,'run_worker',return_value=snapshot), \
                patch.object(privileged,'unit_snapshot',return_value={'ActiveState':'active','MainPID':'123'}), \
                patch.object(recover,'run_operation') as operation_runner:
            with self.assertRaisesRegex(ControlError,'control_maintenance_stop_required'):
                recover.recover(operation,request_path=None,control_manifest_sha256='f'*64)
        operation_runner.assert_not_called()

    def test_operation_observer_and_result_keep_the_same_pinned_reader(self):
        operation=str(uuid.uuid4());active={'ActiveState':'active','MainPID':'123','InvocationID':'1'*32}
        ended={**active,'ActiveState':'inactive','MainPID':'0'}
        with patch.object(administration,'require_root'),patch.object(administration,'publish_request'), \
                patch.object(administration,'operation_unit',side_effect=[active,ended]), \
                patch.object(administration,'run_worker',side_effect=[{'stage':'reserved'},
                    {'completed':True,'stage':'succeeded'}]) as reader, \
                patch.object(administration.time,'sleep'),redirect_stdout(io.StringIO()):
            administration.run_operation({'operation_id':operation},start=False,reader_operation_id=operation)
        self.assertEqual(reader.call_count,2)
        for call in reader.call_args_list:self.assertEqual(call.kwargs['reader_operation_id'],operation)

    def test_control_publication_precedes_os_receipt_in_original_recovery_observer(self):
        operation=str(uuid.uuid4())
        payload={'operation_id':operation,'action':'provision_control','activation_mode':'cutover','expected_generation':7}
        grant={'schema_version':3,'request':payload,'recovery_review_id':str(uuid.uuid4())}
        inactive={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        active={'ActiveState':'active','MainPID':'123','InvocationID':'1'*32}
        events=[];prepared=object()
        with tempfile.TemporaryDirectory() as directory,patch.object(administration,'OPERATIONS',Path(directory)), \
                patch.object(administration,'require_root'),patch.object(administration,'descriptor',return_value=SimpleNamespace(service_gid=998)), \
                patch.object(administration,'operation_unit',side_effect=[inactive,active,inactive]), \
                patch('lifecycle.control.stop._require_empty'),patch.object(administration,'atomic_root_json'), \
                patch.object(administration,'run_worker',side_effect=[{'stage':'recovery_required','os_effect_requested':True},
                    {'completed':True,'stage':'succeeded'}]),patch.object(administration.time,'sleep'), \
                patch.object(control_activation,'prepared_for',return_value=prepared), \
                patch.object(control_activation,'activate',side_effect=lambda *args:events.append('control')) as activate, \
                patch.object(administration,'_apply_fixed_boot_effect',side_effect=lambda *args,**kwargs:events.append('receipt')), \
                redirect_stdout(io.StringIO()):
            administration.run_operation(payload,start=False,recovery_grant=grant,reader_operation_id=operation)
        self.assertEqual(events,['control','receipt'])
        activate.assert_called_once_with(prepared,grant)

    def test_root_pin_publication_is_immutable_and_bound_to_installed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control=root/'control';operations=root/'operations'
            identity=str(uuid.uuid4());operation=str(uuid.uuid4());bundle=control/'versions'/identity
            bundle.mkdir(parents=True);raw=b'{"verified":"manifest"}'
            digest=hashlib.sha256(raw).hexdigest()
            def root_directory(path,**kwargs):
                path.mkdir(parents=True,exist_ok=True)
                return os.open(path,os.O_RDONLY|os.O_DIRECTORY)
            with patch.object(entry,'CONTROL',control),patch.object(entry,'_read',return_value=raw), \
                    patch.object(administration,'OPERATIONS',operations),patch.object(administration,'require_root'), \
                    patch.object(administration,'root_directory',side_effect=root_directory), \
                    patch.object(administration,'descriptor',return_value=SimpleNamespace(service_gid=998)), \
                    patch.object(administration,'read_protected_json',side_effect=lambda path:json.loads(path.read_bytes())), \
                    patch.object(administration,'atomic_root_json',side_effect=lambda path,value,**kwargs:path.write_text(json.dumps(value))) as write:
                administration.publish_operation_reader(operation,bundle=bundle,manifest_sha256=digest)
                administration.publish_operation_reader(operation,bundle=bundle,manifest_sha256=digest)
                self.assertEqual(write.call_count,1)
                path=operations/operation/'reader.json'
                self.assertEqual(json.loads(path.read_bytes()),{'schema_version':1,'operation_id':operation,
                    'bundle_id':identity,'manifest_sha256':digest})
                path.write_text('{}')
                with self.assertRaisesRegex(ControlError,'control_operation_reader_changed'):
                    administration.publish_operation_reader(operation,bundle=bundle,manifest_sha256=digest)
                with self.assertRaisesRegex(ControlError,'control_bundle_mismatch'):
                    administration.publish_operation_reader(operation,bundle=bundle,manifest_sha256='f'*64)
                self.assertEqual(path.read_bytes(),b'{}')
