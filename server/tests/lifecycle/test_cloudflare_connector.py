"""Real inactive secret/reference files; root/OS/vendor boundaries substituted."""
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid
from types import SimpleNamespace

from lifecycle import cloudflare_connector as metadata
from lifecycle.configure_wizard import installation_wizard
from lifecycle.terminal import Console
from lifecycle.control import cloudflare_connector as root,cloudflare_units
from lifecycle.control.rpc import ControlError


class CloudflareConnectorTests(unittest.TestCase):
    def setUp(self):
        self.directory=self.enterContext(tempfile.TemporaryDirectory());self.base=Path(self.directory)
        self.registry=self.base/'cloudflare/connectors'
        self.enterContext(patch.object(metadata,'REGISTRY',self.registry))
        self.enterContext(patch.object(root,'require_root'))
        self.enterContext(patch.object(root,'require_supported_platform'))
        self.manifest={'support':{'schema_version':13,'cloudflare_connector_versions':[1]}}
        self.token=b'SyntheticCloudflareCredentialForIsolatedTestsOnly0001'
        self.input=self.base/'input-token';self.input.write_bytes(self.token+b'\n');self.input.chmod(0o600)

    def draft(self):return root.draft(self.input,'Existing media tunnel',manifest=self.manifest)

    def test_draft_is_read_only_and_never_exposes_token_in_metadata_or_repr(self):
        value=self.draft()
        self.assertEqual(value.token,self.token);self.assertFalse(self.registry.exists())
        self.assertNotIn(self.token.decode(),repr(value));self.assertNotIn(self.token.decode(),json.dumps(value.connector.payload()))
        self.assertEqual(metadata.parse(value.connector.payload()),value.connector)
        self.assertEqual(self.input.read_bytes(),self.token+b'\n')

    def test_capability_root_and_private_regular_file_are_required_before_secret_read(self):
        for support in ({},{'schema_version':12,'cloudflare_connector_versions':[1]},
                {'schema_version':13,'cloudflare_connector_versions':[True]}):
            with self.subTest(support=support),patch.object(root.os,'open') as read,self.assertRaises(ControlError):
                root.draft(self.input,'Media',manifest={'support':support})
            read.assert_not_called()
        self.input.chmod(0o644)
        with self.assertRaisesRegex(ControlError,'control_tunnel_token_file_untrusted'):self.draft()
        self.input.chmod(0o600);alias=self.base/'alias';alias.symlink_to(self.input)
        with self.assertRaises(OSError):root.draft(alias,'Media',manifest=self.manifest)
        alias.unlink();os.link(self.input,alias)
        with self.assertRaises(ControlError):self.draft()

    def test_invalid_token_label_identity_and_pins_cannot_be_published(self):
        for token in (b'short',b'X'*9000,b'X'*40+b'\nY',b'X'*40+b' --flag'):
            self.input.write_bytes(token)
            with self.subTest(length=len(token)),self.assertRaises(ControlError):self.draft()
        value=metadata.Connector(str(uuid.uuid4()),'Media').payload()
        for change in ({'schema_version':True},{'connector_id':'../foreign'},{'connector_id':str(uuid.UUID(int=0))},
                {'label':'--unsafe\nname'},{'binary_version':'latest'},{'binary_sha256':'f'*64},{'token':'sensitive'}):
            with self.subTest(change=change),self.assertRaises(ControlError):metadata.parse({**value,**change})
        invalid=root.DraftConnector(metadata.parse(value),b'short')
        with patch.object(root,'ensure_binary') as binary,self.assertRaises(ControlError):root.register(invalid,manifest=self.manifest)
        binary.assert_not_called()

    def test_registration_publishes_only_inactive_immutable_reference_and_private_token(self):
        value=self.draft();calls=[]
        def directory(path,**kwargs):
            self.assertTrue(path.is_relative_to(self.base))
            current=self.base
            for part in path.relative_to(self.base).parts:
                current/=part;current.mkdir(mode=0o755,exist_ok=True)
            return os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        def write(path,raw,**kwargs):
            self.assertTrue(path.is_relative_to(self.base));path.write_bytes(raw);path.chmod(kwargs.get('mode',0o644))
            calls.append(path.name)
        def protected(path,*,directory=False):
            observed=path.lstat()
            if not path.is_relative_to(self.base):return observed  # Explicit host-ancestor/root-UID substitute.
            if observed.st_mode&0o7022 or (not stat.S_ISDIR(observed.st_mode) if directory else not stat.S_ISREG(observed.st_mode)):
                raise metadata.entry.EntryError('Untrusted test fixture')
            return observed
        with patch.object(root,'administrative_lock',return_value=nullcontext()), \
                patch.object(root,'root_directory',side_effect=directory), \
                patch.object(root,'ensure_binary') as binary,patch.object(root,'ensure_units') as units, \
                patch.object(root,'atomic_root_bytes',side_effect=write), \
                patch.object(root,'atomic_root_json',side_effect=lambda path,value,**kwargs:write(path,json.dumps(value).encode(),**kwargs)), \
                patch.object(metadata.entry,'_protected',side_effect=protected),patch.object(root,'systemctl') as systemctl:
            self.assertEqual(root.register(value,manifest=self.manifest),value.connector)
            self.assertEqual(metadata.inventory(),(value.connector,))
        binary.assert_called_once();units.assert_called_once();systemctl.assert_not_called()
        target=self.registry/value.connector.connector_id
        self.assertEqual((target/'token').read_bytes(),self.token)
        self.assertEqual((target/'token').stat().st_mode&0o777,0o600)
        self.assertEqual(calls,['token','metadata.json'])
        self.assertEqual(self.input.read_bytes(),self.token+b'\n')

    def test_optional_service_uses_dynamic_uid_private_credentials_and_no_global_install(self):
        units=cloudflare_units.unit_files()
        self.assertEqual(set(units),{'/etc/systemd/system/streamhome-tunnel@.service',
            '/etc/systemd/system/streamhome-tunnel-reconcile.service'})
        tunnel=units['/etc/systemd/system/streamhome-tunnel@.service'].decode()
        self.assertIn('DynamicUser=yes',tunnel);self.assertIn('LoadCredential=tunnel-token:',tunnel)
        self.assertIn('--token-file %d/tunnel-token',tunnel);self.assertNotIn('--token ',tunnel)
        self.assertNotIn('service install',tunnel);self.assertNotIn('/usr/bin/cloudflared',tunnel)
        self.assertIn('PartOf=streamhome.service',tunnel)

    def test_installation_decline_discards_token_draft_without_registering_or_downloading(self):
        pending={};references,stage=root.wizard_options(self.manifest,pending)
        answers='\ncloudflare\nhttps://media.example.invalid\n\n\n'+str(self.input)+'\n\nn\n'
        output=io.StringIO();console=Console(io.StringIO(answers),output,interactive=True)
        with patch.object(root,'register') as register,patch.object(root,'ensure_binary') as binary:
            result=installation_wizard(console,diagnostics=lambda config:[],connectors=references,connector_draft=stage)
        self.assertIsNone(result);self.assertEqual(len(pending),1);self.assertFalse(self.registry.exists())
        register.assert_not_called();binary.assert_not_called()
        self.assertNotIn(self.token.decode(),output.getvalue())

    def test_confirmed_installation_still_waits_for_explicit_caller_publication(self):
        pending={};references,stage=root.wizard_options(self.manifest,pending)
        answers='\ncloudflare\nhttps://media.example.invalid\n\n\n'+str(self.input)+'\n\n\n'
        output=io.StringIO();console=Console(io.StringIO(answers),output,interactive=True)
        result=installation_wizard(console,diagnostics=lambda config:[],connectors=references,connector_draft=stage)
        self.assertIsNotNone(result);self.assertFalse(self.registry.exists())
        value=pending[result.deployment.cloudflare_connector_id]
        with patch.object(root,'register',return_value=value.connector) as register, \
                patch.object(metadata,'read',return_value=value.connector):
            self.assertEqual(root.finalize_draft(result.deployment,pending,manifest=self.manifest),value.connector)
        register.assert_called_once_with(value,manifest=self.manifest)
        self.assertEqual(references,[value.connector.connector_id])


class CloudflareReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.manifest={'support':{'schema_version':13,'cloudflare_connector_versions':[1]}}
        self.enterContext(patch.object(root,'require_root'))
        self.enterContext(patch.object(root,'administrative_lock',return_value=nullcontext()))
        self.chosen=metadata.Connector(str(uuid.uuid4()),'Current')
        self.old=metadata.Connector(str(uuid.uuid4()),'Previous')
        self.context={'installation_id':str(uuid.uuid4()),'generation':4,'invocation':'a'*32,
            'execution_sha256':'b'*64,'deployment':{},'connector_id':self.chosen.connector_id}
        self.marker={'schema_version':1,'installation_id':self.context['installation_id'],'generation':3,
            'invocation':'c'*32,'previous':[],'selected':self.old.connector_id,'stage':'selected'}

    def boundaries(self,context=None):
        self.accepted=self.enterContext(patch.object(root,'_accepted_context',return_value=context or self.context))
        self.enterContext(patch.object(root,'_active',return_value=self.marker))
        self.enterContext(patch.object(metadata,'read',side_effect=lambda identity:
            {self.chosen.connector_id:self.chosen,self.old.connector_id:self.old}[identity]))
        self.owned=self.enterContext(patch.object(root,'_owned_unit'))
        self.binary=self.enterContext(patch.object(root,'_binary_verified'))
        self.enterContext(patch.object(root.entry,'_protected',return_value=SimpleNamespace(st_mode=0o100600)))
        self.enterContext(patch.object(root.entry,'_read',return_value=b'SyntheticCloudflareTokenOnlyForIsolatedTests0001'))
        self.writes=self.enterContext(patch.object(root,'atomic_root_json'))
        self.effects=self.enterContext(patch.object(root,'systemctl'))

    def test_switch_is_write_ahead_only_owned_units_and_never_starts_application(self):
        self.boundaries();events=[]
        self.writes.side_effect=lambda path,value:events.append(('write',value['stage']))
        self.effects.side_effect=lambda *args,**kwargs:events.append(('os',args))
        self.assertEqual(root.reconcile(manifest=self.manifest),0)
        self.assertEqual(events,[('write','applying'),('os',('stop',self.old.unit)),
            ('os',('--no-block','start',self.chosen.unit)),('write','selected')])
        self.assertGreaterEqual(self.accepted.call_count,5)
        self.binary.assert_called_once_with(self.chosen.binary)

    def test_local_accepted_settings_stop_previous_without_starting_a_connector(self):
        self.boundaries({**self.context,'connector_id':None})
        root.reconcile(manifest=self.manifest)
        self.effects.assert_called_once_with('stop',self.old.unit,timeout=30)
        self.binary.assert_not_called()
        self.assertEqual(self.writes.call_args.args[1]['selected'],None)

    def test_changed_acceptance_refuses_before_any_effect(self):
        self.boundaries();self.accepted.side_effect=[self.context,{**self.context,'generation':5}]
        with self.assertRaisesRegex(ControlError,'control_tunnel_context_changed'):root.reconcile(manifest=self.manifest)
        self.effects.assert_not_called();self.writes.assert_not_called()

    def test_context_uses_uid_verified_active_settings_and_admitted_backend(self):
        from lifecycle.control import administration,privileged
        from lifecycle.deployment_config import default_deployment
        selected=SimpleNamespace(status=SimpleNamespace(value='ready'),installation_id=self.context['installation_id'])
        unit={'ActiveState':'active','MainPID':'123','InvocationID':'a'*32}
        active=default_deployment().payload()
        value={'installation_id':selected.installation_id,'runtime_verified':True,
            'lifecycle':{'generation':4,'active_operation':None},'execution':{'accepted':'verified UID projection'},
            'active_deployment':active,'desired_deployment':{'unaccepted':'must not be used'}}
        processes={'initial_service_state':'running','backend':{'pid':456},'supervisor_invocation_id':'a'*32}
        with patch.object(privileged,'descriptor',return_value=selected), \
                patch.object(privileged,'unit_snapshot',return_value=unit),patch.object(privileged,'run_worker',return_value=value), \
                patch.object(administration,'_verify_owned_units'),patch.object(administration,'process_snapshot',return_value=processes):
            result=root._accepted_context()
            self.assertEqual(result['deployment'],active);self.assertIsNone(result['connector_id'])
            value['lifecycle']['active_operation']={'phase':'verifying'}
            self.assertIsNone(root._accepted_context())
            value['lifecycle']['active_operation']=None;processes['backend']=None
            self.assertIsNone(root._accepted_context())

    def test_queued_job_does_not_block_or_start_the_application_and_requires_owned_dispatcher(self):
        from lifecycle.control import privileged
        with patch.object(root.Path,'exists',return_value=True), \
                patch.object(privileged,'unit_snapshot',return_value={'ActiveState':'active'}), \
                patch.object(root.entry,'_read',return_value=cloudflare_units.RECONCILE_UNIT.encode()) as read, \
                patch.object(root,'systemctl') as effect:
            root.queue(manifest=self.manifest)
            effect.assert_called_once_with('--no-block','start',root.RECONCILE,timeout=15)
            effect.reset_mock();read.return_value=b'Foreign dispatcher'
            with self.assertRaisesRegex(ControlError,'control_tunnel_unit_untrusted'):root.queue(manifest=self.manifest)
            effect.assert_not_called()

    def test_stop_or_failed_start_retains_write_ahead_selection_for_safe_retry(self):
        self.boundaries();self.effects.side_effect=ControlError('control_systemd_failed')
        with self.assertRaises(ControlError):root.reconcile(manifest=self.manifest)
        self.writes.assert_called_once()
        retained=self.writes.call_args.args[1]
        self.assertEqual(retained['stage'],'applying');self.assertEqual(retained['previous'],[self.old.connector_id])
        self.assertEqual(retained['selected'],self.chosen.connector_id)

    def test_foreign_installation_marker_never_stops_any_service(self):
        self.boundaries();self.marker['installation_id']=str(uuid.uuid4())
        with self.assertRaisesRegex(ControlError,'control_tunnel_selection_untrusted'):root.reconcile(manifest=self.manifest)
        self.effects.assert_not_called();self.writes.assert_not_called()

    def test_optional_units_cannot_pull_in_or_restart_the_primary_application(self):
        for value in cloudflare_units.unit_files().values():
            text=value.decode();self.assertIn('Requisite=streamhome.service',text)
            self.assertNotIn('Requires=streamhome.service',text);self.assertNotIn('BindsTo=streamhome.service',text)
            self.assertNotIn('WantedBy=',text)
        self.assertIn('CapabilityBoundingSet=CAP_SETUID CAP_SETGID',cloudflare_units.RECONCILE_UNIT)

    def test_read_only_commands_never_queue_reconciliation(self):
        from lifecycle.control import privileged
        with patch.object(privileged,'_cli',return_value=0),patch.object(root,'queue_after_change') as queue:
            for arguments in ([],['status'],['doctor'],['version'],['tunnel','list'],['recover','--review']):
                privileged.cli(arguments,manifest=self.manifest,manifest_sha256='a'*64)
        queue.assert_not_called()

    def test_manifest_capability_is_checked_before_token_file_is_opened_by_cli(self):
        from lifecycle.control import privileged
        with patch.object(root.os,'open') as opened,self.assertRaises(ControlError):
            privileged.cli(['tunnel','add','--token-file','/unused'],manifest={'support':{}},manifest_sha256='a'*64)
        opened.assert_not_called()

    def test_active_marker_rejects_confused_types_and_duplicate_or_foreign_keys(self):
        with patch.object(root.Path,'lstat'),patch('lifecycle.control.tickets.read_protected_json',return_value=self.marker) as reader:
            self.assertEqual(root._active(),self.marker)
            for changes in ({'schema_version':True},{'generation':True},{'previous':[self.old.connector_id]*2},
                    {'selected':'../foreign'},{'extra':'unexpected'},{'previous':[self.old.connector_id]}):
                reader.return_value={**self.marker,**changes}
                with self.subTest(changes=changes),self.assertRaises(ControlError):root._active()


class CloudflareDownloadTests(unittest.TestCase):
    def test_hash_mismatch_never_publishes_an_executable_and_retains_inert_download(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);version=base/metadata.VERSION;version.mkdir()
            response=io.BytesIO(b'Synthetic vendor bytes'*100);response.status=200;response.url='https://example.invalid/asset'
            with patch.object(metadata,'BINARY_ROOT',base),patch.object(root,'require_root'), \
                    patch.object(root,'root_directory',side_effect=lambda path,**kwargs:os.open(path,os.O_RDONLY|os.O_DIRECTORY)), \
                    patch.object(root.urllib.request,'build_opener',return_value=SimpleNamespace(open=lambda *args,**kwargs:response)), \
                    patch.object(root,'_binary_verified') as verify,self.assertRaisesRegex(ControlError,'control_tunnel_download_untrusted'):
                root.ensure_binary()
            verify.assert_not_called();self.assertFalse((version/'cloudflared').exists())
            retained=list(version.iterdir());self.assertEqual(len(retained),1)
            self.assertEqual(retained[0].stat().st_mode&0o777,0o600)

    def test_matching_hash_is_verified_before_publish_and_existing_binary_is_not_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);version=base/metadata.VERSION;version.mkdir();raw=b'Synthetic vendor bytes'*100
            response=io.BytesIO(raw);response.status=200;response.url='https://example.invalid/asset'
            opener=SimpleNamespace(open=lambda *args,**kwargs:response)
            with patch.object(metadata,'BINARY_ROOT',base),patch.object(metadata,'SHA256',hashlib.sha256(raw).hexdigest()), \
                    patch.object(root,'require_root'),patch.object(root,'root_directory',side_effect=lambda path,**kwargs:os.open(path,os.O_RDONLY|os.O_DIRECTORY)), \
                    patch.object(root.urllib.request,'build_opener',return_value=opener) as download,patch.object(root,'_binary_verified') as verify:
                root.ensure_binary();root.ensure_binary()
            download.assert_called_once();self.assertEqual(verify.call_count,2)
            target=version/'cloudflared';self.assertEqual(target.read_bytes(),raw)
            self.assertEqual(target.stat().st_mode&0o777,0o755)


if __name__=='__main__':unittest.main()
