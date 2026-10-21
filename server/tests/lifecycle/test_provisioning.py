from copy import deepcopy
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import json
import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import provisioning_contract as contract
from lifecycle.control import provisioning
from lifecycle.control.provisioning_mirror import _prefix
from lifecycle.control import administration_lock
from lifecycle.control import privileged
from lifecycle.control.rpc import ControlError
from lifecycle.control.units import unit_files
from lifecycle.deployment_config import default_deployment


def initial_intent():
    return contract.ProvisioningIntent(
        str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), 999, 999,
        default_deployment().payload(), True, str(uuid.uuid4()), 'a' * 64,
    )


def prepared_intent():
    intent = contract.advance_intent(initial_intent(), stage='account_ready')
    bindings = tuple((name, 5, index + 1) for index, name in enumerate(contract.SERVICE_DIRECTORIES))
    mirrors = (('provisioning', 5, 20), ('provisioning/' + intent.installation_id, 5, 21))
    intent = contract.advance_intent(intent, stage='layout_ready', directories=bindings, mirror_directories=mirrors)
    intent = contract.advance_intent(intent, stage='source_selected', application_commit='b' * 40)
    intent = contract.advance_intent(intent, stage='preparing_application')
    return contract.advance_intent(intent, stage='application_prepared', execution_sha256='c' * 64)


class ProvisioningContractTests(unittest.TestCase):
    def test_roundtrip_retains_frozen_source_and_all_directory_bindings(self):
        intent = prepared_intent()
        self.assertEqual(contract.parse_intent(json.loads(json.dumps(intent.payload()))), intent)
        self.assertEqual(set(dict((name, (device, inode)) for name, device, inode in intent.directories)), set(contract.SERVICE_DIRECTORIES))

    def test_wrong_review_or_replaced_directory_cannot_resume(self):
        intent = prepared_intent()
        for patch_value in ({'operation_id': str(uuid.uuid4())}, {'application_commit': 'd' * 40},
                            {'control_manifest_sha256': 'e' * 64}, {'start_at_boot': False},
                            {'deployment': {**intent.deployment, 'web_port': 9001, 'public_origin': 'http://localhost:9001'}},
                            {'directories': tuple((name, device, inode + 1) for name, device, inode in intent.directories)}):
            with self.subTest(patch_value=patch_value), self.assertRaises(ControlError):
                contract.advance_intent(intent, **patch_value)

    def test_registration_cannot_skip_preparation_or_launch_consumption(self):
        for intent, changes in ((initial_intent(), {'stage': 'registered'}),
                                (prepared_intent(), {'stage': 'registered'}),
                                (prepared_intent(), {'stage': 'first_start_requested'})):
            with self.subTest(stage=intent.stage), self.assertRaises(ControlError):
                contract.advance_intent(intent, **changes)

    def test_first_start_permit_is_consumed_and_cannot_be_rearmed_or_rebound(self):
        intent = contract.advance_intent(prepared_intent(), stage='first_start_requested', start_permit='issued',
            start_boot_id=str(uuid.uuid4()), start_stop_epoch='f' * 64)
        consumed = contract.advance_intent(intent, start_permit='consumed', start_invocation='1' * 32)
        for changes in ({'start_permit': 'issued', 'start_invocation': None},
                        {'start_invocation': '2' * 32}, {'start_boot_id': str(uuid.uuid4())},
                        {'start_stop_epoch': '3' * 64}, {'stage': 'application_prepared'}):
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                contract.advance_intent(consumed, **changes)
        registered = contract.advance_intent(consumed, stage='registered')
        self.assertEqual(contract.advance_intent(registered, stage='complete').start_permit, 'consumed')

    def test_malformed_evidence_and_unknown_destinations_are_rejected(self):
        original = prepared_intent().payload()
        changes = ({'service_uid': True}, {'service_gid': 0}, {'schema_version': True},
                   {'application_commit': 'main'}, {'execution_sha256': 'bad'}, {'start_at_boot': 1},
                   {'start_invocation': '0' * 32}, {'extra': 'data'},
                   {'directories': {**original['directories'], '/tmp/unreviewed': {'device': 1, 'inode': 2}}},
                   {'directories': {**original['directories'], '/opt/streamhome': {'device': True, 'inode': 2}}})
        for changeset in changes:
            with self.subTest(changes=changeset), self.assertRaises((ControlError, ValueError)):
                contract.parse_intent({**deepcopy(original), **changeset})

    def test_root_mirror_may_only_reconcile_an_immutable_monotonic_prefix(self):
        prepared = prepared_intent()
        starting = contract.advance_intent(prepared, stage='first_start_requested', start_permit='issued',
            start_boot_id=str(uuid.uuid4()), start_stop_epoch='f' * 64)
        consumed = contract.advance_intent(starting, start_permit='consumed', start_invocation='1' * 32)
        self.assertTrue(_prefix(prepared, consumed))
        self.assertFalse(_prefix(consumed, prepared))
        self.assertFalse(_prefix(prepared, replace(consumed, installation_id=str(uuid.uuid4()))))
        self.assertFalse(_prefix(prepared, replace(consumed, execution_sha256='e' * 64)))
        changed = tuple((name, device, inode+1) for name, device, inode in consumed.mirror_directories)
        self.assertFalse(_prefix(prepared, replace(consumed, mirror_directories=changed)))


class ProvisioningBoundariesTests(unittest.TestCase):
    def test_direct_ready_start_checks_administrator_exclusion_before_scheduling_observer(self):
        order = []
        @contextmanager
        def lock():
            order.append('acquire')
            yield
            order.append('release')
        selected = SimpleNamespace(status=provisioning.InstallationStatus.READY)
        with patch.object(provisioning, 'require_root'), patch.object(provisioning.Path, 'read_text', return_value='0::/system.slice/streamhome.service\n'), patch.object(provisioning, 'descriptor', return_value=selected), patch.object(provisioning, 'administrative_lock', side_effect=lock), patch('lifecycle.control.startup.schedule_pending', side_effect=lambda: order.append('schedule')):
            self.assertEqual(provisioning.before_start(), 0)
        self.assertEqual(order, ['acquire', 'release', 'schedule'])
        with patch.object(provisioning, 'require_root'), patch.object(provisioning.Path, 'read_text', return_value='0::/system.slice/streamhome.service\n'), patch.object(provisioning, 'descriptor', return_value=selected), patch.object(provisioning, 'administrative_lock', side_effect=ControlError('control_administrator_busy')), patch('lifecycle.control.startup.schedule_pending') as schedule:
            with self.assertRaisesRegex(ControlError, 'control_administrator_busy'):
                provisioning.before_start()
        schedule.assert_not_called()

    def test_doctor_reports_actual_settlement_and_nonzero_for_broken_artifacts(self):
        selected=SimpleNamespace(status=provisioning.InstallationStatus.READY,paths=SimpleNamespace(runtime=Path('/run/streamhome')),service_uid=999,installation_id=str(uuid.uuid4()))
        unit={'ActiveState':'active','SubState':'running','UnitFileState':'enabled','InvocationID':'a'*32}
        supervisor={'error_code':None,'children':{'backend':{'pid':201,'start_identity':'41'}}}
        output=io.StringIO()
        with patch.object(privileged,'inspect_installation',return_value=object()), patch.object(privileged,'descriptor',return_value=selected), patch.object(privileged,'unit_snapshot',return_value=unit), patch.object(privileged.os,'geteuid',return_value=0), patch.object(privileged,'supervisor_snapshot',return_value=supervisor), patch.object(privileged,'request',return_value={'admitted':True,'readiness':{'generalReady':True}}), patch.object(privileged,'run_worker',return_value={'runtime_verified':False}), patch.object(privileged.sys,'stdout',output):
            self.assertEqual(privileged.print_status(detail=True),1)
        self.assertIn('Application readiness: general',output.getvalue())

    def test_version_reads_application_identity_only_through_unprivileged_worker(self):
        output=io.StringIO()
        identity={'installation_id':str(uuid.uuid4()),'application_commit':'a'*40,'application_build_id':'a'*12,'control_bundle_id':str(uuid.uuid4())}
        with patch.object(privileged.os,'geteuid',return_value=0), patch.object(privileged,'run_worker',return_value=identity) as worker, patch.object(privileged.sys,'stdout',output):
            self.assertEqual(privileged.cli(['version'],manifest={'bundle_id':identity['control_bundle_id']},manifest_sha256='b'*64),0)
        worker.assert_called_once_with(['identity'])
        self.assertIn('Application build: '+'a'*12,output.getvalue())
        self.assertIn('Management mode: systemd',output.getvalue())

    def test_doctor_before_descriptor_uses_only_protected_provisioning_evidence(self):
        intent = prepared_intent()
        output = io.StringIO()
        with patch.object(privileged,'inspect_installation',return_value=None), patch.object(provisioning,'inspect_installation',return_value=None), patch.object(privileged.os,'geteuid',return_value=0), patch.object(provisioning,'read_intent',return_value=intent), patch.object(provisioning,'require_root'), patch.object(privileged,'run_worker') as worker, patch.object(privileged.sys,'stdout',output), patch.object(privileged.sys,'stderr',io.StringIO()):
            self.assertEqual(privileged.print_status(detail=True,manifest={'bundle_id':intent.control_bundle_id},manifest_sha256=intent.control_manifest_sha256),3)
        self.assertIn('Recorded installation stage: application_prepared',output.getvalue())
        worker.assert_not_called()

    def test_failure_guidance_uses_only_exact_trusted_control_intent(self):
        intent = prepared_intent()
        output = io.StringIO()
        with patch.object(provisioning, 'require_root'), patch.object(provisioning, 'read_intent', return_value=intent), patch.object(provisioning, 'inspect_installation', return_value=None), patch.object(provisioning.sys, 'stderr', output):
            provisioning.report_continuation(manifest={'bundle_id':intent.control_bundle_id}, manifest_sha256=intent.control_manifest_sha256)
            self.assertIn('--resume-install '+intent.installation_id, output.getvalue())
            output.seek(0)
            output.truncate()
            provisioning.report_continuation(manifest={'bundle_id':str(uuid.uuid4())}, manifest_sha256=intent.control_manifest_sha256)
            self.assertEqual(output.getvalue(), '')

    def test_administrator_takeover_needs_release_or_exact_old_process_death(self):
        boot = str(uuid.uuid4())
        owner = {'schema_version':1,'pid':200,'start_identity':'15','boot_id':boot,'nonce':'a'*64,'status':'active'}
        with patch.object(administration_lock, 'process_identity', return_value=('15','S')):
            self.assertFalse(administration_lock.previous_owner_released(owner, boot))
            self.assertTrue(administration_lock.previous_owner_released({**owner,'status':'released'}, boot))
            self.assertTrue(administration_lock.previous_owner_released(owner, str(uuid.uuid4())))
        for observed in (('16','S'), ('15','Z'), ('15','X')):
            with patch.object(administration_lock, 'process_identity', return_value=observed):
                self.assertTrue(administration_lock.previous_owner_released(owner, boot))
        with patch.object(administration_lock, 'process_identity', side_effect=FileNotFoundError):
            self.assertTrue(administration_lock.previous_owner_released(owner, boot))
        with patch.object(administration_lock, 'process_identity', side_effect=PermissionError), self.assertRaises(PermissionError):
            administration_lock.previous_owner_released(owner, boot)

    def test_root_provisioner_does_not_import_application_or_canonical_authority(self):
        code = "import sys; import lifecycle.control.provisioning; import lifecycle.control.units; assert not {'lifecycle.state','lifecycle.management_journal','lifecycle.configuration_store','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registered_account_is_never_automatically_recreated(self):
        intent = prepared_intent()
        with patch.object(provisioning.grp, 'getgrnam', side_effect=KeyError), patch.object(provisioning, '_fixed_os') as effect:
            with self.assertRaises(ControlError):
                provisioning._ensure_account(intent)
        effect.assert_not_called()

    def test_first_start_permit_is_persisted_before_systemctl_start(self):
        intent = prepared_intent()
        order = []
        def persist(previous, **changes):
            self.assertEqual(changes['start_permit'], 'issued')
            order.append('permit')
            return contract.advance_intent(previous, **changes)
        def start(*arguments, **kwargs):
            self.assertEqual(order, ['permit'])
            self.assertEqual(arguments, ('start', 'streamhome.service'))
            order.append('start')
            raise ControlError('test_interruption')
        with patch.object(provisioning, '_advance', side_effect=persist), patch('lifecycle.control.administration.root_stop_epoch', return_value='d' * 64), patch.object(provisioning, 'unit_snapshot', return_value={'ActiveState': 'inactive', 'MainPID': '0'}), patch.object(provisioning, 'systemctl', side_effect=start):
            with self.assertRaises(ControlError):
                provisioning._first_start(intent,expected_stop_epoch='d'*64)
        self.assertEqual(order, ['permit', 'start'])

    def test_consumed_first_start_never_starts_a_new_or_stopped_invocation(self):
        intent = contract.advance_intent(prepared_intent(), stage='first_start_requested', start_permit='issued',
            start_boot_id=str(uuid.uuid4()), start_stop_epoch='d' * 64)
        intent = contract.advance_intent(intent, start_permit='consumed', start_invocation='1' * 32)
        for active, invocation in [('inactive', ''), ('active', '2' * 32)]:
            with self.subTest(active=active), patch('lifecycle.control.administration.root_stop_epoch', return_value='d' * 64), patch.object(provisioning, 'systemctl') as effect, patch.object(provisioning, 'unit_snapshot', return_value={'ActiveState': active, 'MainPID': '200', 'InvocationID': invocation}):
                with self.assertRaises(ControlError):
                    provisioning._first_start(intent,expected_stop_epoch='d'*64)
                effect.assert_not_called()

    def test_first_start_rejects_a_stop_during_preparation_or_an_older_permit(self):
        intent = prepared_intent()
        issued = contract.advance_intent(intent, stage='first_start_requested', start_permit='issued',
            start_boot_id=str(uuid.uuid4()), start_stop_epoch='d' * 64)
        for selected, expected in ((intent, 'd' * 64), (issued, 'e' * 64)):
            with self.subTest(permit=selected.start_permit), patch('lifecycle.control.administration.root_stop_epoch', return_value='e' * 64), patch.object(provisioning, '_advance') as persist, patch.object(provisioning, 'unit_snapshot') as observe, patch.object(provisioning, 'systemctl') as start:
                with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
                    provisioning._first_start(selected, expected_stop_epoch=expected)
                persist.assert_not_called()
                observe.assert_not_called()
                start.assert_not_called()

    def test_resume_captures_stop_before_long_preparation_and_never_reseeds_it(self):
        prepared = prepared_intent()
        intent = replace(prepared, stage='preparing_application', execution_sha256=None)
        epoch = ['d' * 64]
        def prepare(current):
            self.assertEqual(current, intent)
            epoch[0] = 'e' * 64
            return {'installation_id': intent.installation_id, 'operation_id': intent.operation_id,
                'execution_sha256': prepared.execution_sha256, 'application_commit': intent.application_commit}
        selected = SimpleNamespace(descriptor=SimpleNamespace(status=provisioning.InstallationStatus.PROVISIONING))
        marker = SimpleNamespace(exists=lambda: True, is_symlink=lambda: False)
        real_path = Path
        def paths(value):
            return marker if value == '/etc/streamhome/control/stop-epoch.json' else real_path(value)
        with patch.object(provisioning, 'require_root'), patch.object(provisioning, 'require_supported_platform'), patch.object(provisioning, '_require_install_scope'), patch.object(provisioning, 'administrative_lock', return_value=nullcontext()), patch.object(provisioning, 'read_intent', return_value=intent), patch.object(provisioning, 'inspect_installation', return_value=selected), patch.object(provisioning, 'Path', side_effect=paths), patch('lifecycle.control.administration.root_stop_epoch', side_effect=lambda: epoch[0]), patch.object(provisioning, '_ensure_account'), patch('lifecycle.control.provisioning_mirror.publish_mirror'), patch.object(provisioning, '_validate_service_directories'), patch.object(provisioning, '_publish_provisional_descriptor'), patch.object(provisioning, 'run_worker', return_value={'generation': 0}), patch.object(provisioning, '_prepare_application', side_effect=prepare), patch.object(provisioning, '_advance', side_effect=contract.advance_intent), patch.object(provisioning, 'atomic_root_json') as seed, patch.object(provisioning, 'systemctl') as start, patch.object(provisioning.sys, 'stdout', io.StringIO()):
            with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
                provisioning.resume_install(intent.installation_id,
                    manifest={'bundle_id': intent.control_bundle_id}, manifest_sha256=intent.control_manifest_sha256, scope_epoch='d' * 64)
        seed.assert_not_called()
        start.assert_not_called()

    def test_parent_releases_administration_before_scope_and_never_prepares_or_launches(self):
        intent = prepared_intent()
        held = []
        @contextmanager
        def lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()
        def handoff(installation, epoch):
            self.assertFalse(held)
            self.assertEqual((installation, epoch), (intent.installation_id, 'd' * 64))
            return 0
        selected = SimpleNamespace(descriptor=SimpleNamespace(status=provisioning.InstallationStatus.PROVISIONING))
        marker = SimpleNamespace(exists=lambda: True, is_symlink=lambda: False)
        real_path = Path
        def paths(value):
            return marker if value == '/etc/streamhome/control/stop-epoch.json' else real_path(value)
        with patch.object(provisioning, 'require_root'), patch.object(provisioning, 'require_supported_platform'), patch.object(provisioning, 'administrative_lock', side_effect=lock), patch.object(provisioning, 'read_intent', return_value=intent), patch.object(provisioning, 'inspect_installation', return_value=selected), patch.object(provisioning, 'Path', side_effect=paths), patch('lifecycle.control.administration.root_stop_epoch', return_value='d' * 64), patch.object(provisioning, '_ensure_account'), patch('lifecycle.control.provisioning_mirror.publish_mirror'), patch.object(provisioning, '_validate_service_directories'), patch.object(provisioning, '_publish_provisional_descriptor') as publish, patch.object(provisioning, 'run_worker') as worker, patch.object(provisioning, '_first_start') as start, patch.object(provisioning, '_run_install_scope', side_effect=handoff) as scope:
            self.assertEqual(provisioning.resume_install(intent.installation_id, manifest={'bundle_id': intent.control_bundle_id}, manifest_sha256=intent.control_manifest_sha256), 0)
        scope.assert_called_once()
        publish.assert_not_called()
        worker.assert_not_called()
        start.assert_not_called()

    def test_scoped_issuer_requires_exact_current_kernel_scope(self):
        installation = str(uuid.uuid4())
        name = 'streamhome-install-' + installation + '.scope'
        metadata = 'ActiveState=active\nTransient=yes\nControlGroup=/system.slice/' + name
        with patch.object(provisioning.Path, 'read_text', return_value='0::/system.slice/' + name + '\n'), patch.object(provisioning, 'systemctl', return_value=metadata):
            provisioning._require_install_scope(installation, 'd' * 64)
        with patch.object(provisioning.Path, 'read_text', return_value='0::/user.slice/untracked.scope\n'), patch.object(provisioning, 'systemctl') as ctl:
            with self.assertRaisesRegex(ControlError, 'control_installation_scope_invalid'):
                provisioning._require_install_scope(installation, 'd' * 64)
        ctl.assert_not_called()

    def test_scope_failure_never_relaunches_and_cleans_separate_builder(self):
        intent = prepared_intent()
        with patch.object(provisioning.subprocess, 'run', return_value=SimpleNamespace(returncode=143)) as launch, patch.object(provisioning, '_stop_install_scope_work') as cleanup:
            with self.assertRaisesRegex(ControlError, 'control_installation_scope_stopped'):
                provisioning._run_install_scope(intent.installation_id, 'd' * 64)
        arguments = launch.call_args.args[0]
        self.assertIn('--property=StopPropagatedFrom=streamhome.service', arguments)
        self.assertIn('--resume-install-scoped', arguments)
        self.assertNotIn('shell', launch.call_args.kwargs)
        self.assertEqual(launch.call_count, 1)
        cleanup.assert_called_once_with(intent.installation_id)

        name = 'streamhome-install-' + intent.installation_id + '.scope'
        worker = 'streamhome-provision@' + intent.operation_id + '.service'
        with patch.object(provisioning, 'read_intent', return_value=intent), patch.object(provisioning, 'systemctl', side_effect=['not-found', 'loaded', '']) as ctl, patch('lifecycle.control.stop._require_empty') as empty:
            provisioning._stop_install_scope_work(intent.installation_id)
        self.assertEqual(ctl.call_args.args, ('stop', worker))
        self.assertEqual([call.args[0] for call in empty.call_args_list], [name, worker])

    def test_preparation_and_controller_units_are_unprivileged_and_distinct(self):
        files = unit_files()
        runtime = files['/etc/systemd/system/streamhome.service'].decode()
        self.assertIn('ExecStartPre=+/usr/local/libexec/streamhome/provision --before-start', runtime)
        self.assertIn('ExecStopPost=+/usr/local/libexec/streamhome/provision --unit-stopped', runtime)
        for unit, argument in [('operation', '--operation'), ('provision', '--provision-worker')]:
            value = files[f'/etc/systemd/system/streamhome-{unit}@.service'].decode()
            self.assertIn('User=streamhome\nGroup=streamhome\nSlice=system.slice\n', value)
            self.assertIn(f'ExecStart=/usr/local/libexec/streamhome/provision {argument} %i', value)
            self.assertIn('KillMode=control-group', value)
            self.assertIn('Restart=no', value)
            self.assertIn('StopPropagatedFrom=streamhome.service', value)
            self.assertNotIn('BindsTo=', value)
        self.assertNotIn('streamhome-provision@', files['/etc/polkit-1/rules.d/49-streamhome-operations.rules'].decode())


if __name__ == '__main__':
    unittest.main()
