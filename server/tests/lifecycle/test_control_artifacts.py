"""Real Ed25519/dpkg envelopes; verification never installs or executes files."""
import fcntl
from copy import deepcopy
from dataclasses import asdict
from contextlib import nullcontext
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import shutil
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import artifacts, artifact_stage, entry, control_activation, control_selection
from lifecycle.control.rpc import ControlError


class ControlArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.marker = cls.root/'must-not-execute'
        source = cls.root/'source/server/lifecycle'
        for name in ('__init__.py','state.py','control/__init__.py','control/privileged.py','control/entry.py','control/units.py'):
            path = source/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('from pathlib import Path\nPath('+repr(str(cls.marker))+').write_text("executed")\n')
        from lifecycle.control import units
        (source/'control/units.py').write_bytes(Path(units.__file__).read_bytes())
        cls.key = cls.root/'publisher-private.pem'
        subprocess.run(['/usr/bin/openssl','genpkey','-algorithm','ED25519','-out',str(cls.key)],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.key.chmod(0o600)
        build_path = Path(__file__).resolve().parents[3]/'scripts/build_control_package.py'
        specification = importlib.util.spec_from_file_location('artifact_test_builder',build_path)
        builder = importlib.util.module_from_spec(specification);specification.loader.exec_module(builder)
        cls.builder=builder
        cls.output = cls.root/'release'
        cls.release = builder.build(cls.root/'source',cls.output,cls.key,str(uuid.uuid4()))
        cls.package = (cls.output/'streamhome-control-bootstrap.deb').read_bytes()
        cls.raw_release = (cls.output/'release.json').read_bytes()
        cls.signature = (cls.output/'release.sig').read_bytes()
        cls.public = (cls.output/'publisher.pem').read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.enterContext(patch.object(artifacts,'PUBLISHER_SHA256',hashlib.sha256(self.public).hexdigest()))

    def verify(self,package=None,release=None,signature=None,key=None):
        return artifacts.verify_bytes(self.package if package is None else package,
            self.raw_release if release is None else release,
            self.signature if signature is None else signature,self.public if key is None else key)

    def signed(self,package,*,release=None):
        value={**self.release,'artifact_sha256':hashlib.sha256(package).hexdigest(),**(release or {})}
        raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            path=Path(directory)/'release';path.write_bytes(raw)
            result=subprocess.run(['/usr/bin/openssl','pkeyutl','-sign','-rawin','-inkey',str(self.key),'-in',str(path)],
                check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        return raw,result.stdout

    def changed_data(self,change):
        archives=artifacts._archives(self.package)
        with tarfile.open(fileobj=io.BytesIO(archives['data.tar.gz']),mode='r:gz') as archive:
            entries=[(member,archive.extractfile(member).read() if member.isfile() else None) for member in archive]
        change(entries)
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w:gz') as archive:
            for member,data in entries:archive.addfile(member,io.BytesIO(data) if data is not None else None)
        archives['data.tar.gz']=output.getvalue()
        result=bytearray(b'!<arch>\n')
        for name,raw in archives.items():
            header=f'{name+"/":<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(raw):<10}`\n'.encode()
            self.assertEqual(len(header),60)
            result.extend(header);result.extend(raw)
            if len(raw)%2:result.extend(b'\n')
        return bytes(result)

    def test_actual_builder_signature_and_exact_paired_module_inventory_verify_without_execution(self):
        result=self.verify()
        self.assertEqual(result.bundle_id,self.release['bundle_id'])
        self.assertEqual(result.manifest_sha256,self.release['manifest_sha256'])
        self.assertIn('usr/local/bin/streamhome',result.files)
        self.assertFalse(self.marker.exists())
        with self.assertRaises(TypeError):result.files['unexpected']=b'code'
        self.assertEqual(artifacts.verify_local(self.output/'streamhome-control-bootstrap.deb'),result)

    def test_wrong_key_signature_or_package_bytes_cannot_reach_archive_validation(self):
        with patch.object(artifacts,'_archives') as parse:
            for package,signature,key in ((self.package,self.signature,self.public+b'changed'),
                    (self.package,b'0'*64,self.public),(self.package+b'changed',self.signature,self.public)):
                with self.subTest(size=len(package)),self.assertRaises(ControlError):
                    self.verify(package=package,signature=signature,key=key)
            parse.assert_not_called()

    def test_upgrade_support_checks_actual_reader_formats_and_preserves_prior_platform(self):
        verified=self.verify()
        manifest=json.loads(verified.files[artifacts.CONTROL+'versions/'+verified.bundle_id+'/manifest.json'])
        artifacts.require_upgrade_support(manifest,verified)
        older=deepcopy(manifest);older['support']['schema_version']=9
        older['support'].pop('bound_control_repair_versions')
        older['support'].pop('cloudflare_connector_versions')
        older['support']['recovery_request_versions']=[1,2,3,4]
        older['support']['managed_parent_recovery_versions']=[1,2,3]
        older['support']['maintenance_actions']=['repair_bound_environment']
        older['support'].pop('control_maintenance_versions');older['support']['resumption_plan_versions']=[1,2]
        for name in ('managed_update_versions','managed_update_recovery_versions','managed_update_environment_repair_versions'):
            older['support'].pop(name)
        artifacts.require_upgrade_support(older,verified)
        different_units=deepcopy(manifest)
        different_units['files']['lib/lifecycle/control/units.py']='f'*64
        with self.assertRaisesRegex(ControlError,'control_units_incompatible'):
            artifacts.require_upgrade_support(different_units,verified)
        for change in ({'entry_protocol':True},{'authority_versions':[True,2]},
                {'control_protocol':2},{'support':{**manifest['support'],'schema_version':14}},
                {'support':{**manifest['support'],'platform':'unqualified-platform'}}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                artifacts.require_upgrade_support({**manifest,**change},verified)
        self.assertFalse(self.marker.exists())

    def test_signature_inputs_are_sealed_before_the_verifier_os_process(self):
        original=subprocess.run;observed=[]
        def run(*args,**kwargs):
            for descriptor in kwargs['pass_fds']:
                seals=fcntl.fcntl(descriptor,fcntl.F_GET_SEALS)
                self.assertTrue(seals & fcntl.F_SEAL_WRITE)
                self.assertTrue(seals & fcntl.F_SEAL_GROW)
                self.assertTrue(seals & fcntl.F_SEAL_SHRINK)
                observed.append(descriptor)
            return original(*args,**kwargs)
        with patch.object(artifacts.subprocess,'run',side_effect=run):self.verify()
        self.assertEqual(len(observed),3)
        for descriptor in observed:
            with self.assertRaises(OSError):os.fstat(descriptor)

    def test_signed_extra_privileged_destination_alias_or_duplicate_is_rejected(self):
        def extra(entries):
            member=tarfile.TarInfo('./etc/cron.d/unrequested');member.size=4;member.mode=0o644
            entries.append((member,b'code'))
        def alias(entries):
            member=tarfile.TarInfo('./usr/local/libexec/streamhome/alias')
            member.type=tarfile.SYMTYPE;member.linkname='/etc';member.mode=0o755
            entries.append((member,None))
        def duplicate(entries):entries.append(next(item for item in entries if item[0].isfile()))
        for change in (extra,alias,duplicate):
            package=self.changed_data(change);release,signature=self.signed(package)
            with self.subTest(change=change.__name__),self.assertRaises(ControlError):
                self.verify(package=package,release=release,signature=signature)
        self.assertFalse(self.marker.exists())

    def test_signed_corrupt_rescue_copy_is_rejected(self):
        def corrupt(entries):
            for index,(member,data) in enumerate(entries):
                if member.isfile() and '/rescue-versions/' in member.name and member.name.endswith('/state.py'):
                    replacement=data+b'changed';member.size=len(replacement);entries[index]=(member,replacement);return
            self.fail('Fixture rescue module missing')
        package=self.changed_data(corrupt);release,signature=self.signed(package)
        with self.assertRaises(ControlError):self.verify(package=package,release=release,signature=signature)

    def test_signed_global_entry_must_be_identical_to_the_manifest_bound_module(self):
        replacement=b'raise RuntimeError("signed but unrelated dispatcher")\n'
        def different_entry(entries):
            for index,(member,data) in enumerate(entries):
                if member.name.removeprefix('./')==artifacts.CONTROL+'provision':
                    member.size=len(replacement);entries[index]=(member,replacement);return
            self.fail('Global entry absent from fixture')
        package=self.changed_data(different_entry)
        release,signature=self.signed(package,release={'entry_sha256':hashlib.sha256(replacement).hexdigest()})
        with self.assertRaises(ControlError):self.verify(package=package,release=release,signature=signature)
        self.assertFalse(self.marker.exists())

    def test_signed_selection_cannot_use_boolean_schema_version(self):
        def change(entries):
            for index,(member,data) in enumerate(entries):
                if member.name.endswith('/bootstrap-selection.json'):
                    value=json.loads(data);value['schema_version']=True
                    replacement=json.dumps(value).encode();member.size=len(replacement)
                    entries[index]=(member,replacement);return
            self.fail('Fixture selection missing')
        package=self.changed_data(change);release,signature=self.signed(package)
        with self.assertRaises(ControlError):self.verify(package=package,release=release,signature=signature)

    def test_release_representation_and_archive_resource_limits_fail_closed(self):
        for changes in ({'schema_version':True},{'artifact':'different.deb'},
                {'publisher_sha256':'0'*64},{'bundle_id':str(uuid.UUID(int=0))}):
            release,signature=self.signed(self.package,release=changes)
            with self.subTest(changes=changes),self.assertRaises(ControlError):
                self.verify(release=release,signature=signature)
        import gzip
        with self.assertRaises(ControlError):artifacts._tar(gzip.compress(b'x'*1025),limit=1024)
        with self.assertRaises(ControlError):artifacts._archives(b'!<arch>\n' + b'incomplete')

    def test_local_artifact_does_not_follow_a_file_alias(self):
        alias=self.root/('alias-'+uuid.uuid4().hex)
        alias.symlink_to(self.output/'streamhome-control-bootstrap.deb')
        with self.assertRaises(OSError):artifacts.verify_local(alias)

    def stage_fixture(self, *, real_lock=False):
        """Substitute only root ownership/lock; copy/fsync/signatures stay real."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
        control = root/'control';control.mkdir(mode=0o755)
        def protected(path, *, directory=False):
            metadata=path.lstat()
            if directory and path in control.parents:return metadata
            if (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7022
                    or not (stat.S_ISDIR(metadata.st_mode) if directory else
                        stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1)):
                raise ControlError('test_untrusted_path')
            return metadata
        def directory(path, *, create=False, **kwargs):
            self.assertTrue(path.is_relative_to(control))
            if create:path.mkdir(mode=kwargs.get('mode',0o755),parents=True,exist_ok=True)
            protected(path,directory=True)
            return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        self.enterContext(patch.object(artifact_stage,'CONTROL',control))
        self.enterContext(patch.object(artifact_stage,'root_directory',side_effect=directory))
        self.enterContext(patch.object(entry,'_protected',side_effect=protected))
        self.enterContext(patch('lifecycle.control.privileged.require_root'))
        if not real_lock:
            self.enterContext(patch('lifecycle.control.provisioning.administrative_lock',side_effect=lambda **kwargs:nullcontext()))
        return control

    def test_preparation_reuses_only_its_existing_kernel_administrator_lock_without_reissuing_owner(self):
        from lifecycle.control import provisioning,administration_lock
        control=self.stage_fixture(real_lock=True)
        owner={'schema_version':1,'pid':os.getpid(),'start_identity':'123','boot_id':str(uuid.uuid4()),
            'nonce':'a'*64,'status':'active'}
        original_fstat=os.fstat
        def fstat(descriptor):
            metadata=original_fstat(descriptor)
            value={name:getattr(metadata,name) for name in dir(metadata) if name.startswith('st_')}
            return SimpleNamespace(**{**value,'st_uid':0})
        with patch.object(provisioning,'require_root'),patch.object(provisioning,'root_directory',
                side_effect=lambda path,**kwargs:os.open(control,os.O_RDONLY|os.O_DIRECTORY)), \
                patch.object(provisioning.os,'fstat',side_effect=fstat), \
                patch.object(provisioning,'read_protected_json',return_value=owner), \
                patch.object(administration_lock,'enter_owner',return_value=owner) as enter, \
                patch.object(administration_lock,'release_owner') as release:
            self.assertIsNone(provisioning._ADMINISTRATIVE_CONTEXT)
            with provisioning.administrative_lock():
                descriptor=provisioning._ADMINISTRATIVE_CONTEXT['handle']
                with self.assertRaisesRegex(ControlError,'control_administrator_busy'):
                    with provisioning.administrative_lock():self.fail('Independent nested lock acquired')
                prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
                self.assertEqual(provisioning._ADMINISTRATIVE_CONTEXT['handle'],descriptor)
                os.fstat(descriptor)
                enter.assert_called_once();release.assert_not_called()
                artifact_stage.require_prepared(prepared)
            self.assertIsNone(provisioning._ADMINISTRATIVE_CONTEXT)
            release.assert_called_once_with(owner)
            with self.assertRaises(OSError):os.fstat(descriptor)

    def test_inactive_preparation_preserves_selection_and_reuses_exact_copies(self):
        control=self.stage_fixture()
        for name in ('selected.json','rescue.json','provision'):(control/name).write_bytes(b'unchanged')
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        version=control/'versions'/prepared.bundle_id
        original_inode=version.stat().st_ino
        self.assertEqual(artifact_stage.require_prepared(prepared),self.verify())
        self.assertEqual(artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb'),prepared)
        self.assertEqual(version.stat().st_ino,original_inode)
        for name in ('selected.json','rescue.json','provision'):self.assertEqual((control/name).read_bytes(),b'unchanged')
        self.assertFalse(self.marker.exists())

    def test_envelope_only_cache_never_loads_or_replaces_corrupt_bound_normal_code(self):
        control=self.stage_fixture()
        target=control/'versions'/self.release['bundle_id'];target.mkdir(mode=0o755,parents=True)
        broken=target/'damaged';broken.write_bytes(b'preserve damaged control')
        prepared=artifact_stage.cache_local(self.output/'streamhome-control-bootstrap.deb')
        artifact_stage.require_cached(prepared)
        self.assertEqual(broken.read_bytes(),b'preserve damaged control')
        self.assertFalse((control/'rescue-versions').exists())
        self.assertFalse((control/'selected.json').exists())
        self.assertFalse(self.marker.exists())

    def test_preparation_refuses_and_preserves_existing_corruption(self):
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        module=control/'versions'/prepared.bundle_id/'lib/lifecycle/state.py'
        module.write_bytes(b'corrupted')
        with self.assertRaises(ControlError):artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        self.assertEqual(module.read_bytes(),b'corrupted')
        with self.assertRaises(ControlError):artifact_stage.require_prepared(prepared)

    def test_incompatible_upgrade_is_refused_before_any_inactive_publication(self):
        control=self.stage_fixture()
        manifest=json.loads(self.verify().files[artifacts.CONTROL+'versions/'+self.release['bundle_id']+'/manifest.json'])
        manifest['support']['platform']='unqualified-platform'
        with self.assertRaises(ControlError):
            artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb',previous_manifest=manifest)
        self.assertEqual(list(control.iterdir()),[])

    def test_private_caller_umask_does_not_make_published_code_unreadable_to_service_uid(self):
        control=self.stage_fixture()
        previous=os.umask(0o077)
        try:
            prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
            artifact_stage.require_prepared(prepared)
        finally:os.umask(previous)
        for path in (control/'versions'/prepared.bundle_id).rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777,0o755 if path.is_dir() else 0o644)
        version=control/'versions'/prepared.bundle_id;version.chmod(0o700)
        with self.assertRaises(ControlError):artifact_stage.require_prepared(prepared)
        self.assertEqual(version.stat().st_mode & 0o777,0o700)

    def test_prepared_proof_rejects_cache_or_rescue_changes_and_extra_directories(self):
        for role in ('cache','rescue','extra'):
            with self.subTest(role=role):
                control=self.stage_fixture()
                prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
                if role=='cache':(control/'artifacts'/prepared.artifact_sha256/'release.sig').write_bytes(b'0'*64)
                elif role=='rescue':(control/'rescue-versions'/prepared.bundle_id/'lib/lifecycle/state.py').write_bytes(b'corrupted')
                else:(control/'versions'/prepared.bundle_id/'extra').mkdir()
                with self.assertRaises(ControlError):artifact_stage.require_prepared(prepared)

    def test_interrupted_preparation_never_publishes_partial_version(self):
        control=self.stage_fixture()
        original=artifact_stage._sync
        def interrupted(path):
            if path.parent == control/'staging':raise OSError('simulated publication interruption')
            return original(path)
        with patch.object(artifact_stage,'_sync',side_effect=interrupted),self.assertRaises(OSError):
            artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        self.assertFalse((control/'versions').exists())
        retained=set((control/'staging').iterdir());self.assertTrue(retained)
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        self.assertTrue(retained <= set((control/'staging').iterdir()))
        artifact_stage.require_prepared(prepared)

    def test_prepared_proof_rejects_path_aliases_and_forged_identity(self):
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        module=control/'rescue-versions'/prepared.bundle_id/'lib/lifecycle/state.py'
        module.unlink();module.symlink_to(control/'versions'/prepared.bundle_id/'lib/lifecycle/state.py')
        with self.assertRaises(ControlError):artifact_stage.require_prepared(prepared)
        for value in (None,artifact_stage.PreparedControl('../escape','0'*64,'0'*64,'0'*64)):
            with self.subTest(value=value),self.assertRaises(ControlError):artifact_stage.require_prepared(value)

    def test_operation_pin_keeps_original_verified_reader_after_selection_changes(self):
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        (control/'provision').write_bytes(b'protected test entry')
        (control/'selected.json').write_text(json.dumps({'schema_version':1,
            'bundle_id':str(uuid.uuid4()),'manifest_sha256':'f'*64}))
        operations=control/'operations';operations.mkdir(mode=0o755)
        operation=str(uuid.uuid4());directory=operations/operation;directory.mkdir(mode=0o755)
        pin={'schema_version':1,'operation_id':operation,'bundle_id':prepared.bundle_id,
            'manifest_sha256':prepared.manifest_sha256}
        (directory/'reader.json').write_text(json.dumps(pin))
        for path in (control/'provision',control/'selected.json',directory/'reader.json'):path.chmod(0o644)
        self.enterContext(patch.object(entry,'CONTROL',control))
        self.enterContext(patch.object(entry,'OPERATIONS',operations))
        self.enterContext(patch.object(entry,'__file__',str(control/'provision')))
        self.enterContext(patch.object(entry.sys,'path',list(entry.sys.path)))
        bundle,manifest,digest=entry.load_bundle(operation_id=operation)
        self.assertEqual(bundle,control/'versions'/prepared.bundle_id)
        self.assertEqual(digest,prepared.manifest_sha256)
        self.assertEqual(manifest['bundle_id'],prepared.bundle_id)
        self.assertFalse(self.marker.exists())
        with self.assertRaises(FileNotFoundError):entry.load_bundle()
        pin['operation_id']=str(uuid.uuid4());(directory/'reader.json').write_text(json.dumps(pin))
        with self.assertRaises((entry.EntryError,ControlError)):entry.load_bundle(operation_id=operation)
        (directory/'reader.json').unlink()
        with self.assertRaises(entry.EntryError):entry.load_bundle(operation_id=operation,require_operation_pin=True)
        (directory/'reader.json').symlink_to(directory/'absent')
        with self.assertRaises((entry.EntryError,ControlError)):entry.load_bundle(operation_id=operation)

    def test_installed_bundle_verification_is_data_only_and_refuses_changed_modules(self):
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        self.enterContext(patch.object(entry,'CONTROL',control))
        selection={'schema_version':1,'bundle_id':prepared.bundle_id,'manifest_sha256':prepared.manifest_sha256}
        original=list(entry.sys.path);bytecode=entry.sys.dont_write_bytecode
        bundle,manifest,digest=entry.verify_bundle(selection)
        self.assertEqual(bundle,control/'versions'/prepared.bundle_id)
        self.assertEqual(manifest['bundle_id'],prepared.bundle_id)
        self.assertEqual(digest,prepared.manifest_sha256)
        self.assertEqual(entry.sys.path,original)
        self.assertEqual(entry.sys.dont_write_bytecode,bytecode)
        self.assertFalse(self.marker.exists())
        (bundle/'lib/lifecycle/state.py').write_bytes(b'changed')
        with self.assertRaises(entry.EntryError):entry.verify_bundle(selection)
        self.assertEqual(entry.sys.path,original)

    def test_exact_operation_rescue_uses_only_independent_same_identity_copy_without_fallback(self):
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        (control/'provision').write_bytes(b'protected test entry')
        operations=control/'operations';operations.mkdir(mode=0o755)
        operation=str(uuid.uuid4());directory=operations/operation;directory.mkdir(mode=0o755)
        (directory/'reader.json').write_text(json.dumps({'schema_version':1,'operation_id':operation,
            'bundle_id':prepared.bundle_id,'manifest_sha256':prepared.manifest_sha256}))
        for path in (control/'provision',directory/'reader.json'):path.chmod(0o644)
        self.enterContext(patch.object(entry,'CONTROL',control))
        self.enterContext(patch.object(entry,'OPERATIONS',operations))
        self.enterContext(patch.object(entry,'__file__',str(control/'provision')))
        self.enterContext(patch.object(entry.sys,'path',list(entry.sys.path)))
        (control/'versions'/prepared.bundle_id/'lib/lifecycle/state.py').write_bytes(b'damaged normal copy')
        with self.assertRaises(entry.EntryError):entry.load_bundle(operation_id=operation,require_operation_pin=True)
        with self.assertRaises(entry.EntryError):entry.load_bundle(rescue=True,operation_id=operation,require_operation_pin=True)
        bundle,_,digest=entry.load_bundle(rescue=True,operation_id=operation,require_operation_pin=True,operation_rescue=True)
        self.assertEqual(bundle,control/'rescue-versions'/prepared.bundle_id)
        self.assertEqual(digest,prepared.manifest_sha256)
        (bundle/'lib/lifecycle/state.py').write_bytes(b'damaged independent copy')
        with self.assertRaises(entry.EntryError):
            entry.load_bundle(rescue=True,operation_id=operation,require_operation_pin=True,operation_rescue=True)
        self.assertFalse(self.marker.exists())

    def test_explicit_control_repair_uses_selected_same_identity_rescue_without_borrowing_global_rescue_pointer(self):
        repair,control,target,selection,records=self.reconstruction_fixture()
        (control/'provision').write_bytes(b'protected test dispatcher')
        (control/'selected.json').write_text(json.dumps(selection))
        (control/'rescue.json').write_text(json.dumps({'schema_version':1,'bundle_id':str(uuid.uuid4()),'manifest_sha256':'f'*64}))
        for name in ('provision','selected.json','rescue.json'):(control/name).chmod(0o644)
        self.enterContext(patch.object(entry,'__file__',str(control/'provision')))
        self.enterContext(patch.object(entry,'OPERATIONS',control/'operations'))
        self.enterContext(patch.object(entry.sys,'path',list(entry.sys.path)))
        operation=str(uuid.uuid4())
        with self.assertRaises(entry.EntryError):entry.load_bundle()
        with self.assertRaises(FileNotFoundError):entry.load_bundle(rescue=True)
        bundle,_,digest=entry.load_bundle(rescue=True,operation_id=operation,bound_control_repair=True)
        self.assertEqual(bundle,control/'rescue-versions'/selection['bundle_id'])
        self.assertEqual(digest,selection['manifest_sha256'])
        self.assertFalse(self.marker.exists())

    def test_repair_actor_role_is_root_pinned_generation_bound_and_finite_without_granting_canonical_permission(self):
        repair,control,target,selection,records=self.reconstruction_fixture()
        operation=str(uuid.uuid4());operations=control/'operations';directory=operations/operation
        operations.mkdir(mode=0o755);directory.mkdir(mode=0o755)
        (directory/'reader.json').write_text(json.dumps({**selection,'operation_id':operation}))
        grant={'schema_version':5,'action':'repair_bound_control','request':{'operation_id':operation,
            'expected_generation':1,'controller_control_manifest_sha256':selection['manifest_sha256'],
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()},'expires_at_monotonic':200}
        raw=json.dumps(grant,sort_keys=True,separators=(',',':')).encode()
        (directory/'recovery-2.json').write_bytes(raw)
        role={'schema_version':1,'operation_id':operation,'selection':selection,'generation':2,
            'grant_sha256':hashlib.sha256(raw).hexdigest(),'stage':'active'}
        path=directory/'control-repair-reader.json';path.write_text(json.dumps(role))
        for p in directory.iterdir():p.chmod(0o644)
        self.enterContext(patch.object(entry,'OPERATIONS',operations))
        with patch.object(entry.time,'monotonic',return_value=100):
            self.assertTrue(entry.control_repair_role(operation))
            for change in ({'grant_sha256':'f'*64},{'generation':True},{'stage':[]},
                    {'selection':{**selection,'schema_version':True}}):
                path.write_text(json.dumps({**role,**change}))
                with self.subTest(change=change),self.assertRaises(entry.EntryError):entry.control_repair_role(operation)
            path.write_text(json.dumps(role))
        with patch.object(entry.time,'monotonic',return_value=201),self.assertRaises(entry.EntryError):
            entry.control_repair_role(operation)
        path.write_text(json.dumps({**role,'stage':'complete'}))
        with patch.object(entry.time,'monotonic',return_value=201):self.assertFalse(entry.control_repair_role(operation))
        self.assertEqual((target/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')

    def activation_fixture(self, *, previous=False, unit_change=False):
        """Actual signed/cached trees and file effects; substitute OS/UID proof."""
        control=self.stage_fixture()
        old=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        source=control/'test-source';shutil.copytree(self.root/'source',source)
        module=source/'server/lifecycle/control/entry.py';module.write_bytes(module.read_bytes()+b'# candidate\n')
        if unit_change:
            module=source/'server/lifecycle/control/units.py'
            module.write_bytes(module.read_bytes().replace(b'Description=StreamHome runtime supervisor',
                b'Description=StreamHome managed runtime supervisor'))
        output=control/'test-release'
        self.builder.build(source,output,self.key,str(uuid.uuid4()))
        new=artifact_stage.prepare_local(output/'streamhome-control-bootstrap.deb')
        old_selection={'schema_version':1,'bundle_id':old.bundle_id,'manifest_sha256':old.manifest_sha256}
        (control/'selected.json').write_text(json.dumps(old_selection));(control/'selected.json').chmod(0o644)
        (control/'provision').write_bytes(self.verify().files[artifacts.CONTROL+'provision']);(control/'provision').chmod(0o755)
        operations=control/'operations';operations.mkdir(mode=0o755)
        operation=str(uuid.uuid4());directory=operations/operation;directory.mkdir(mode=0o755)
        pin={**old_selection,'operation_id':operation}
        request={'operation_id':operation,'action':'provision_control','activation_mode':'cutover','initial_service_state':'running','interruption_approved':True,
            'expected_generation':7,'controller_control_manifest_sha256':old.manifest_sha256,
            'previous':{'control_bundle_id':old.bundle_id,'control_manifest_sha256':old.manifest_sha256,'control_protocol_version':1},
            'candidate':{'control_bundle_id':new.bundle_id,'control_manifest_sha256':new.manifest_sha256,'control_protocol_version':1}}
        intent={'schema_version':1,'request_sha256':control_activation._sha(request),'prepared':asdict(new)}
        self.activation_grant={'schema_version':3,'request':request,'source_request_sha256':control_activation._sha(request)}
        for name,value in (('reader.json',pin),('control-artifact.json',intent),('request.json',request)):
            (directory/name).write_text(json.dumps(value));(directory/name).chmod(0o644)
        selected=request['previous' if previous else 'candidate']
        plan={'schema_version':3,'action':'provision_control','generation':8,
            'stages':[{'execution_sha256':'e'*64,'control_manifest_sha256':selected['control_manifest_sha256']}]}
        observation={'request_sha256':control_activation._sha(request),'generation':8,'controller_pid':123,
            'controller_invocation_id':'2'*32,'stage':'recovery_required','os_effect_requested':True,
            'selected_execution_sha256':'e'*64,'selected_control':selected}
        self.enterContext(patch.object(entry,'CONTROL',control))
        self.enterContext(patch.object(entry,'OPERATIONS',operations))
        self.enterContext(patch.object(control_activation,'OPERATIONS',operations))
        self.enterContext(patch.object(control_activation,'require_root'))
        self.enterContext(patch.object(control_activation,'require_stopped'))
        self.enterContext(patch.object(control_activation,'_verify_owned_units'))
        self.enterContext(patch.object(control_activation,'qualify',return_value={'plan':plan,'issuer':{'owner':{'pid':os.getpid()}}}))
        self.enterContext(patch.object(control_activation,'operation_unit',return_value={'ActiveState':'active','MainPID':'123','InvocationID':'2'*32}))
        worker=self.enterContext(patch.object(control_activation,'run_worker',return_value=observation))
        self.enterContext(patch.object(control_activation,'root_directory',side_effect=artifact_stage.root_directory))
        self.enterContext(patch.object(control_activation,'read_protected_json',side_effect=lambda path:json.loads(path.read_bytes())))
        self.enterContext(patch.object(control_activation,'atomic_root_json',side_effect=lambda path,value,**kwargs:path.write_text(json.dumps(value))))
        return control,new,request,worker

    def reconstruction_fixture(self):
        from lifecycle.control import control_reconstruction as repair
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        self.enterContext(patch.object(entry,'CONTROL',control))
        self.enterContext(patch.object(repair,'_metadata',side_effect=lambda path:SimpleNamespace(
            **{**{name:getattr(path.lstat(),name) for name in dir(path.lstat()) if name.startswith('st_')},'st_uid':0})))
        selection={'schema_version':1,'bundle_id':prepared.bundle_id,'manifest_sha256':prepared.manifest_sha256}
        target=control/'versions'/prepared.bundle_id
        (target/'lib/lifecycle/state.py').write_bytes(b'damaged exact normal reader')
        records=[]
        return repair,control,target,selection,records

    def test_exact_normal_control_reconstruction_preserves_damage_and_independent_rescue(self):
        repair,control,target,selection,records=self.reconstruction_fixture()
        expected=repair.fingerprint(target)
        result=repair.reconstruct(self.output/'streamhome-control-bootstrap.deb',selection,
            expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=expected,checkpoint=lambda:None,quiescent=lambda:None,
            record=lambda value:records.append(json.loads(json.dumps(value))))
        self.assertEqual(result['stage'],'complete')
        self.assertEqual([value['stage'] for value in records],['rename_planned','reconstructing','complete'])
        quarantine=target.parent/result['quarantine']
        self.assertEqual((quarantine/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        entry.verify_bundle(selection);entry.verify_bundle(selection,rescue=True)
        self.assertFalse((control/'selected.json').exists())
        self.assertFalse(self.marker.exists())

    def test_uid_bound_review_can_authenticate_cached_envelope_and_independent_rescue_without_normal_code(self):
        from lifecycle import bound_control_repair
        repair,control,target,selection,records=self.reconstruction_fixture()
        verified=self.verify()
        value={'bundle_id':verified.bundle_id,'manifest_sha256':verified.manifest_sha256,
            'artifact_sha256':verified.artifact_sha256,'entry_sha256':verified.entry_sha256}
        self.assertEqual(bound_control_repair._artifact(value),verified)
        self.assertEqual(records,[])
        self.assertEqual((target/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        (control/'rescue-versions'/selection['bundle_id']/'lib/lifecycle/state.py').write_bytes(b'damaged rescue')
        with self.assertRaises((ControlError,entry.EntryError)):bound_control_repair._artifact(value)
        self.assertFalse(self.marker.exists())

    def test_control_reconstruction_requires_exact_independent_identity_and_fresh_filesystem_review(self):
        repair,control,target,selection,records=self.reconstruction_fixture()
        package=self.output/'streamhome-control-bootstrap.deb'
        for chosen,expected in (({**selection,'bundle_id':str(uuid.uuid4())},repair.fingerprint(target)),
                (selection,'f'*64)):
            with self.subTest(chosen=chosen),self.assertRaises((ControlError,entry.EntryError)):
                repair.reconstruct(package,chosen,expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=expected,checkpoint=lambda:None,
                    quiescent=lambda:None,record=records.append)
        self.assertEqual(records,[])
        self.assertEqual((target/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        (control/'rescue-versions'/selection['bundle_id']/'lib/lifecycle/state.py').write_bytes(b'damaged rescue')
        with self.assertRaises((ControlError,entry.EntryError)):
            repair.reconstruct(package,selection,expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=repair.fingerprint(target),
                checkpoint=lambda:None,quiescent=lambda:None,record=records.append)
        self.assertEqual(records,[])

    def test_interrupted_control_reconstruction_keeps_quarantine_and_requires_new_observation(self):
        repair,control,target,selection,records=self.reconstruction_fixture()
        package=self.output/'streamhome-control-bootstrap.deb';expected=repair.fingerprint(target)
        original=artifact_stage._publish_inactive
        def fail(path,files):raise OSError('simulated copy interruption after preserved rename')
        with patch.object(artifact_stage,'_publish_inactive',side_effect=fail),self.assertRaises(OSError):
            repair.reconstruct(package,selection,expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=expected,checkpoint=lambda:None,
                quiescent=lambda:None,record=records.append)
        retained=target.parent/records[-1]['quarantine']
        self.assertTrue(retained.is_dir());self.assertFalse(target.exists())
        with self.assertRaises(ControlError):
            repair.reconstruct(package,selection,expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=expected,checkpoint=lambda:None,
                quiescent=lambda:None,record=records.append)
        repaired=repair.reconstruct(package,selection,expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=repair.fingerprint(target),
            checkpoint=lambda:None,quiescent=lambda:None,record=records.append)
        self.assertIsNone(repaired['quarantine'])
        self.assertEqual((retained/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        entry.verify_bundle(selection)

    def test_control_reconstruction_loss_of_quiescence_before_rename_changes_nothing(self):
        repair,control,target,selection,records=self.reconstruction_fixture()
        calls=[]
        def quiet():
            calls.append(True)
            if len(calls)==2:raise ControlError('control_foreign_service_process')
        with self.assertRaises(ControlError):
            repair.reconstruct(self.output/'streamhome-control-bootstrap.deb',selection,
                expected_artifact_sha256=self.release['artifact_sha256'],expected_fingerprint=repair.fingerprint(target),checkpoint=lambda:None,quiescent=quiet,record=records.append)
        self.assertEqual((target/'lib/lifecycle/state.py').read_bytes(),b'damaged exact normal reader')
        self.assertEqual([value['stage'] for value in records],['rename_planned'])

    def unit_publication_fixture(self):
        """Real file effects, signed source and replay; substitute fixed OS paths."""
        from lifecycle.control.unit_payload import parse
        control,prepared,request,worker=self.activation_fixture(unit_change=True)
        old_bundle=control/'versions'/request['previous']['control_bundle_id']
        old=parse((old_bundle/'lib/lifecycle/control/units.py').read_bytes())
        new=parse((control/'versions'/prepared.bundle_id/'lib/lifecycle/control/units.py').read_bytes())
        paths={name:control/('unit-'+str(index)) for index,name in enumerate(sorted(old))}
        for name,path in paths.items():path.write_bytes(old[name])
        original_read=entry._read
        def read(path,*args,**kwargs):
            return paths[str(path)].read_bytes() if str(path) in paths else original_read(path,*args,**kwargs)
        self.enterContext(patch.object(entry,'_read',side_effect=read))
        def verify(*,expected=None,alternate=None):
            self.assertEqual(set(expected),set(paths))
            for name,path in paths.items():
                allowed=(expected[name],) if alternate is None else (expected[name],alternate[name])
                if path.read_bytes() not in allowed:raise ControlError('control_units_changed')
        control_activation._verify_owned_units.side_effect=verify
        def write(path,raw,**kwargs):
            destination=paths[str(path)]
            temporary=destination.with_suffix('.pending')
            with temporary.open('xb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
            os.replace(temporary,destination)
        self.enterContext(patch.object(control_activation,'atomic_root_bytes',side_effect=write))
        reload=self.enterContext(patch.object(control_activation,'systemctl',return_value=''))
        return control,prepared,request,worker,old,new,paths,reload

    def test_changed_owned_unit_bytes_publish_before_reload_and_control_selection(self):
        control,prepared,request,worker,old,new,paths,reload=self.unit_publication_fixture()
        def reloaded(*args):
            self.assertEqual(args,('daemon-reload',))
            self.assertEqual({name:path.read_bytes() for name,path in paths.items()},new)
            self.assertEqual(json.loads((control/'selected.json').read_bytes())['bundle_id'],request['previous']['control_bundle_id'])
            return ''
        reload.side_effect=reloaded
        target=control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(target['bundle_id'],prepared.bundle_id)
        reload.assert_called_once_with('daemon-reload')

    def test_failed_reload_retains_finite_unit_bytes_for_fresh_canonical_rollback(self):
        control,prepared,request,worker,old,new,paths,reload=self.unit_publication_fixture()
        reload.side_effect=ControlError('simulated_reload_failure')
        with self.assertRaises(ControlError):control_activation.activate(prepared,self.activation_grant)
        receipt=control/'operations'/request['operation_id']/'control-cutover-8.json'
        self.assertEqual(json.loads(receipt.read_bytes())['stage'],'applying')
        self.assertEqual({name:path.read_bytes() for name,path in paths.items()},new)
        # A fresh generation, chosen by the original canonical owner, rolls back
        # only the control files. The old intent and failed bytes remain evidence.
        request['expected_generation']=8
        directory=receipt.parent
        # Original request/artifact binding must stay immutable across recovery.
        self.activation_grant['source_request_sha256']=json.loads((directory/'control-artifact.json').read_bytes())['request_sha256']
        old_target=request['previous']
        worker.return_value={**worker.return_value,'request_sha256':control_activation._sha(request),
            'generation':9,'selected_control':old_target}
        plan=control_activation.qualify.return_value['plan']
        plan['generation']=9;plan['stages'][0]['control_manifest_sha256']=old_target['control_manifest_sha256']
        reload.side_effect=None
        target=control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(target['bundle_id'],old_target['control_bundle_id'])
        self.assertEqual({name:path.read_bytes() for name,path in paths.items()},old)
        self.assertEqual(json.loads(receipt.read_bytes())['stage'],'applying')

    def test_unknown_owned_unit_bytes_are_preserved_and_refused_before_cutover(self):
        control,prepared,request,worker,old,new,paths,reload=self.unit_publication_fixture()
        next(iter(paths.values())).write_bytes(b'unknown administrator edit')
        pointer=(control/'selected.json').read_bytes()
        with self.assertRaises(ControlError):control_activation.activate(prepared,self.activation_grant)
        reload.assert_not_called()
        self.assertEqual((control/'selected.json').read_bytes(),pointer)

    def test_control_publication_uses_exact_old_uid_decision_and_preserves_inactive_evidence(self):
        control,prepared,request,worker=self.activation_fixture()
        target=control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(target['bundle_id'],prepared.bundle_id)
        self.assertEqual(json.loads((control/'selected.json').read_bytes()),target)
        self.assertTrue((control/'versions'/request['previous']['control_bundle_id']).is_dir())
        self.assertTrue((control/'rescue-versions'/prepared.bundle_id).is_dir())
        self.assertFalse(self.marker.exists())
        self.assertEqual(worker.call_count,4)
        for call in worker.call_args_list:self.assertEqual(call.kwargs['reader_operation_id'],request['operation_id'])

    def test_control_rollback_can_select_verified_previous_even_with_corrupt_candidate_tree(self):
        control,prepared,request,worker=self.activation_fixture(previous=True)
        module=control/'versions'/prepared.bundle_id/'lib/lifecycle/state.py';module.write_bytes(b'corrupted')
        artifact_stage.require_cached(prepared)
        with self.assertRaises(ControlError):artifact_stage.require_prepared(prepared)
        target=control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(target['bundle_id'],request['previous']['control_bundle_id'])
        self.assertEqual(module.read_bytes(),b'corrupted')

    def test_changed_canonical_decision_before_publication_keeps_previous_selection(self):
        control,prepared,request,worker=self.activation_fixture()
        original=(control/'selected.json').read_bytes()
        worker.return_value={**worker.return_value,'selected_execution_sha256':'f'*64}
        with self.assertRaises(ControlError):control_activation.activate(prepared,self.activation_grant)
        self.assertEqual((control/'selected.json').read_bytes(),original)

    def test_speculative_cutover_read_repeats_full_live_proof_before_any_file_effect(self):
        control,prepared,request,worker=self.activation_fixture()
        original=(control/'selected.json').read_bytes();observation=worker.return_value
        calls=[]
        def observe(*args,**kwargs):
            calls.append(True)
            if len(calls)==1:
                self.assertEqual((control/'selected.json').read_bytes(),original)
                raise ControlError('control_worker_failed')
            return observation
        worker.side_effect=observe
        with patch.object(control_activation.time,'sleep'):
            target=control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(target['bundle_id'],prepared.bundle_id)
        self.assertEqual(worker.call_count,5)
        self.assertEqual(control_activation.qualify.call_count,5)

    def test_speculative_cutover_retry_cannot_outlive_issuer_or_renew_permission(self):
        control,prepared,request,worker=self.activation_fixture()
        original=(control/'selected.json').read_bytes()
        permission=control_activation.qualify.return_value
        control_activation.qualify.side_effect=[permission,ControlError('control_resumption_issuer_changed')]
        worker.side_effect=ControlError('control_worker_failed')
        with patch.object(control_activation.time,'sleep'),self.assertRaisesRegex(ControlError,'control_resumption_issuer_changed'):
            control_activation.activate(prepared,self.activation_grant)
        self.assertEqual((control/'selected.json').read_bytes(),original)
        worker.assert_called_once()

    def test_persistent_cutover_read_failure_is_bounded_and_changes_no_control_file(self):
        control,prepared,request,worker=self.activation_fixture()
        before={name:(control/name).read_bytes() for name in ('provision','selected.json')}
        worker.side_effect=ControlError('control_worker_failed')
        with patch.object(control_activation.time,'monotonic',side_effect=[100,101,103]), \
                patch.object(control_activation.time,'sleep'),self.assertRaisesRegex(ControlError,'control_worker_failed'):
            control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(worker.call_count,2)
        self.assertEqual({name:(control/name).read_bytes() for name in before},before)

    def test_interrupted_control_file_pair_retains_intent_and_can_reconcile_exact_bytes(self):
        control,prepared,request,worker=self.activation_fixture()
        pointer=(control/'selected.json').read_bytes()
        original_write=control_activation._write
        def fail_second(name,raw):
            if name=='selected.json':raise OSError('simulated selection publication failure')
            original_write(name,raw)
        with patch.object(control_activation,'_write',side_effect=fail_second),self.assertRaises(OSError):
            control_activation.activate(prepared,self.activation_grant)
        self.assertEqual((control/'selected.json').read_bytes(),pointer)
        receipt=control/'operations'/request['operation_id']/'control-cutover-8.json'
        self.assertEqual(json.loads(receipt.read_bytes())['stage'],'applying')
        target=control_activation.activate(prepared,self.activation_grant)
        self.assertEqual(json.loads(receipt.read_bytes())['stage'],'complete')
        self.assertEqual(json.loads((control/'selected.json').read_bytes()),target)
        self.assertFalse(self.marker.exists())

    def test_lost_issuer_after_intent_cannot_replace_any_control_file(self):
        control,prepared,request,worker=self.activation_fixture()
        before={name:(control/name).read_bytes() for name in ('provision','selected.json')}
        permission=control_activation.qualify.return_value
        with patch.object(control_activation,'qualify',side_effect=[permission,ControlError('lost issuer')]),self.assertRaises(ControlError):
            control_activation.activate(prepared,self.activation_grant)
        self.assertEqual({name:(control/name).read_bytes() for name in before},before)

    def test_prepared_artifact_intent_is_bound_once_to_original_request(self):
        control,prepared,request,worker=self.activation_fixture()
        path=control/'operations'/request['operation_id']/'control-artifact.json'
        path.unlink()
        control_activation.bind_prepared(request,prepared)
        original=path.read_bytes()
        self.assertEqual(control_activation.prepared_for(request),prepared)
        control_activation.bind_prepared(request,prepared)
        self.assertEqual(path.read_bytes(),original)
        path.write_text('{}')
        with self.assertRaises(ControlError):control_activation.bind_prepared(request,prepared)
        self.assertEqual(path.read_bytes(),b'{}')

    def test_selected_control_proof_checks_actual_protected_pointer_entry_and_inventory(self):
        control,prepared,request,worker=self.activation_fixture()
        execution=SimpleNamespace(**request['previous'])
        original=list(entry.sys.path)
        evidence=control_selection.require_selected(execution)
        self.assertEqual(evidence['manifest_sha256'],execution.control_manifest_sha256)
        self.assertEqual(entry.sys.path,original)
        with self.assertRaises(ControlError):control_selection.require_selected(SimpleNamespace(**request['candidate']))
        (control/'provision').chmod(0o644)
        with self.assertRaises(ControlError):control_selection.require_selected(execution)
        (control/'provision').chmod(0o755)
        (control/'provision').write_bytes(b'changed executable')
        with self.assertRaises(ControlError):control_selection.require_selected(execution)
        self.assertFalse(self.marker.exists())

    def test_original_stopped_publication_uses_scope_permission_without_any_resumption_plan(self):
        control,prepared,request,worker=self.activation_fixture()
        request['initial_service_state']='stopped'
        directory=control/'operations'/request['operation_id']
        (directory/'request.json').write_text(json.dumps(request))
        (directory/'control-artifact.json').write_text(json.dumps({'schema_version':1,
            'request_sha256':control_activation._sha(request),'prepared':asdict(prepared)}))
        worker.return_value={**worker.return_value,'request_sha256':control_activation._sha(request),'stage':'prepared'}
        issuer={'issuer':{'owner':{'pid':os.getpid()}}}
        with patch('lifecycle.control.control_permission.require_initial',return_value=issuer) as permission:
            target=control_activation.activate_stopped(prepared,request)
        self.assertEqual(target['bundle_id'],prepared.bundle_id)
        self.assertEqual(permission.call_count,4)
        control_activation.qualify.assert_not_called()

    def test_maintenance_reader_requires_qualified_support_and_complete_protected_inventory(self):
        from lifecycle.control.reader_support import require_control_maintenance_reader
        control=self.stage_fixture()
        prepared=artifact_stage.prepare_local(self.output/'streamhome-control-bootstrap.deb')
        self.enterContext(patch.object(entry,'CONTROL',control))
        execution=SimpleNamespace(control_bundle_id=prepared.bundle_id,control_manifest_sha256=prepared.manifest_sha256,
            control_protocol_version=1)
        original=list(entry.sys.path)
        require_control_maintenance_reader(execution)
        self.assertEqual(entry.sys.path,original)
        with self.assertRaises(ControlError):require_control_maintenance_reader(SimpleNamespace(**{**execution.__dict__,'control_protocol_version':True}))
        path=control/'versions'/prepared.bundle_id/'manifest.json';manifest=json.loads(path.read_bytes())
        manifest['support'].update(schema_version=10,resumption_plan_versions=[1,2]);manifest['support'].pop('control_maintenance_versions')
        path.write_text(json.dumps(manifest))
        execution.control_manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ControlError):require_control_maintenance_reader(execution)
        self.assertFalse(self.marker.exists())
