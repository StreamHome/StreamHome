from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from lifecycle import management_journal as contract
from lifecycle.control import processes, privileged, repair_interruption as interrupt, runtime_preparation as preparation, stopped_repair
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_runtime_preparation import grant, replacement
from tests.lifecycle.test_management_requests import request_payload


def independent_grant():
    value = grant()
    value.update(schema_version=3, backend=None, issuer_scope={
        'unit': 'streamhome-install-' + value['installation_id'] + '-' + '2' * 32 + '.scope',
        'invocation': '3' * 32, 'pid': os.getpid(), 'start_identity': '200'})
    return value


class IndependentRepairGrantTests(unittest.TestCase):
    def test_runtime_custody_refuses_a_replaced_supervisor_queued_job_or_closed_launch_gate(self):
        value = independent_grant();peer = value['supervisor']
        reference = (peer['pid'], peer['start_identity'], value['supervisor_invocation_id'])
        unit = {'ActiveState': 'active', 'MainPID': str(peer['pid']), 'InvocationID': reference[2]}
        def path(name):
            if str(name).startswith('/proc/'):return SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000))
            return SimpleNamespace(read_text=lambda: str(peer['pid']) + ' 222')
        with patch.object(processes.os, 'geteuid', return_value=1000), patch.object(processes, 'Path', side_effect=path), patch.object(processes, 'process_cgroup', return_value=processes.RUNTIME_CGROUP), patch('lifecycle.control.administration_lock.process_identity', return_value=(reference[1], 'S')), patch.object(privileged, 'descriptor', return_value=SimpleNamespace(service_uid=1000)), patch.object(privileged, 'unit_snapshot', return_value=unit), patch.object(privileged, 'systemctl', return_value='Job=') as ctl, patch.object(privileged, 'supervisor_snapshot', return_value={'launches_open': True, 'start_identity': reference[1]}) as snapshot:
            self.assertEqual(processes._running_preparation_members(reference), {peer['pid'], 222})
            ctl.return_value = 'Job=12/stop'
            with self.assertRaises(ControlError):processes._running_preparation_members(reference)
            ctl.return_value = 'Job=';snapshot.return_value['launches_open'] = False
            with self.assertRaises(ControlError):processes._running_preparation_members(reference)
            snapshot.return_value['launches_open'] = True;unit['MainPID'] = '999'
            with self.assertRaises(ControlError):processes._running_preparation_members(reference)

    def test_independent_preparation_never_exempts_foreign_service_uid_work(self):
        class Process:
            def __init__(self, pid):self.name = str(pid)
            def stat(self):return SimpleNamespace(st_uid=1000)
            def __truediv__(self, _):return SimpleNamespace(read_text=lambda: self.name + ' (fixture) S 0')
        peers = [Process(100), Process(101)]
        def path(name):
            if name == '/proc':return SimpleNamespace(iterdir=lambda: peers)
            return SimpleNamespace(read_text=lambda: '100 101')
        value = independent_grant()
        with patch.object(processes.os, 'geteuid', return_value=1000), patch.object(processes.os, 'getpid', return_value=300), patch.object(processes, 'Path', side_effect=path), patch.object(processes, '_running_preparation_members', return_value={100, 101}), patch.object(processes, 'process_cgroup', return_value=processes.RUNTIME_CGROUP), patch.object(processes, '_protected_reader', return_value=False):
            processes.require_service_quiescent(preparation_id=value['preparation_id'], runtime_supervisor=(100, '200', '1' * 32))
            peers.append(Process(102))
            with self.assertRaisesRegex(ControlError, 'control_foreign_service_process'):
                processes.require_service_quiescent(preparation_id=value['preparation_id'], runtime_supervisor=(100, '200', '1' * 32))

    def test_unhealthy_preparation_has_an_exact_supervisor_and_scope_but_no_backend_permission(self):
        value = independent_grant()
        self.assertEqual(preparation.parse_grant(value), value)
        for changed in ({'backend': grant()['backend']}, {'supervisor': None},
                        {'issuer_scope': None}, {'supervisor_invocation_id': None}, {'schema_version': 1}):
            with self.subTest(changed=changed), self.assertRaises((ControlError, ValueError)):
                preparation.parse_grant({**value, **changed})

    def test_running_scope_wrapper_cannot_select_a_recovery_or_arbitrary_dispatch(self):
        value = independent_grant()
        handoff = stopped_repair.StoppedRepairHandoff(value['installation_id'], value['preparation_id'],
            value['stop_epoch'], None, running_repair=True)
        with patch.object(stopped_repair, 'require_root'), patch.object(stopped_repair.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(stopped_repair.run_scoped(handoff), 0)
        args = run.call_args.args[0]
        self.assertIn('--repair-running-scoped', args)
        self.assertIn('--property=StopPropagatedFrom=streamhome.service', args)
        self.assertNotIn('start', args)


class PreparedRepairInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.grant = independent_grant();self.operation = self.grant['preparation_id']
        self.directory = self.root / self.operation;self.directory.mkdir()
        self.payload = request_payload()
        self.payload.update(installation_id=self.grant['installation_id'], operation_id=self.operation,
            expected_generation=self.grant['expected_generation'], action='repair_environment',
            previous=self.grant['previous'], candidate=contract.execution_tuple_payload(replacement(contract.parse_execution_tuple(self.grant['previous']))),
            backend=None, supervisor=self.grant['supervisor'], supervisor_invocation_id=self.grant['supervisor_invocation_id'],
            boot_id=self.grant['boot_id'], stop_epoch=self.grant['stop_epoch'],
            controller_control_manifest_sha256=self.grant['control_manifest_sha256'])
        self.observation = {'stage': 'prepared', 'request_sha256': interrupt._sha(self.payload),
            'generation': self.payload['expected_generation'] + 1, 'controller_pid': 200,
            'controller_invocation_id': '4' * 32}
        self.unit = {'ActiveState': 'active', 'MainPID': str(self.payload['supervisor']['pid']),
                     'InvocationID': self.payload['supervisor_invocation_id']}
        self.actor = {'ActiveState': 'active', 'MainPID': '200', 'InvocationID': '4' * 32}
        def read(path):
            if 'preparations' in path.parts:return self.grant
            if path.name == 'request.json':return self.payload
            if path.name == 'os-receipt.json':return {**self.observation, 'stage': 'complete'}
            return json.loads(path.read_text())
        def path(name):
            if name == '/proc/sys/kernel/random/boot_id':return SimpleNamespace(read_text=lambda: self.payload['boot_id'])
            if str(name).startswith('/proc/'):return SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000))
            return Path(name)
        for name, value in (('OPERATIONS', self.root), ('require_root', lambda: None),
                ('descriptor', lambda: SimpleNamespace(installation_id=self.payload['installation_id'], service_uid=1000)),
                ('read_protected_json', read), ('Path', path), ('require_scope', lambda *args: None),
                ('root_stop_epoch', lambda: self.payload['stop_epoch']), ('operation_unit', lambda _: self.actor),
                ('unit_snapshot', lambda: self.unit), ('process_identity', lambda _: (self.payload['supervisor']['start_identity'], 'S')),
                ('process_cgroup', lambda _: interrupt.RUNTIME_CGROUP),
                ('supervisor_snapshot', lambda *args: {'launches_open': True, 'start_identity': self.payload['supervisor']['start_identity']}),
                ('_verify_owned_units', lambda: None),
                ('atomic_root_json', lambda path, value, **kwargs: path.write_text(json.dumps(value)))):
            self.enterContext(patch.object(interrupt, name, value))
        self.systemctl = self.enterContext(patch.object(interrupt, 'systemctl', return_value='Job='))

    def test_complete_candidate_is_stopped_once_and_receipt_never_authorizes_a_start(self):
        with self.assertRaisesRegex(ControlError, 'control_operation_recovery_required'):
            interrupt.stop_prepared(self.payload, self.observation)
        receipt = json.loads((self.directory / 'repair-stop.json').read_text())
        self.assertEqual(receipt['request_sha256'], interrupt._sha(self.payload))
        self.assertNotIn('start', receipt)
        self.systemctl.assert_any_call('stop', 'streamhome.service', timeout=90)
        self.systemctl.reset_mock()
        with self.assertRaisesRegex(ControlError, 'control_repair_interruption_consumed'):
            interrupt.stop_prepared(self.payload, self.observation)
        self.assertFalse(any(call.args[0] == 'stop' for call in self.systemctl.call_args_list))

    def test_changed_controller_or_original_supervisor_prevents_stop_and_publication(self):
        different_invocation = 'e' * 32 if self.unit['InvocationID'] != 'e' * 32 else 'c' * 32
        for target, changed in ((self.actor, {'MainPID': '201'}), (self.unit, {'InvocationID': different_invocation})):
            old = dict(target);target.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ControlError):
                interrupt.stop_prepared(self.payload, self.observation)
            target.clear();target.update(old)
            self.assertFalse((self.directory / 'repair-stop.json').exists())
        self.assertFalse(any(call.args[0] == 'stop' for call in self.systemctl.call_args_list))

    def test_unprepared_expired_or_backend_borrowing_permission_never_stops_the_service(self):
        for field, value in (('schema_version', 1), ('expires_at_monotonic', 0), ('backend', grant()['backend'])):
            old = deepcopy(self.grant);self.grant[field] = value
            with self.subTest(field=field), self.assertRaises(ControlError):
                interrupt.stop_prepared(self.payload, self.observation)
            self.grant.clear();self.grant.update(old)
        with self.assertRaises(ControlError):
            interrupt.stop_prepared(self.payload, {**self.observation, 'stage': 'verifying'})
        self.systemctl.assert_not_called()

    def test_root_stop_dispatch_imports_no_canonical_or_application_implementation(self):
        code = "import sys;import lifecycle.control.repair_interruption;assert not {'lifecycle.state','lifecycle.management_journal','config','application','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class PreparedControlInterruptionTests(unittest.TestCase):
    def setUp(self):
        from lifecycle.control import artifact_stage
        fixture=PreparedRepairInterruptionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.payload=fixture.payload;self.observation=fixture.observation
        self.payload.update(action='provision_control',backend={'pid':999,'start_identity':'1001'})
        self.payload['candidate']={**self.payload['previous'],'control_bundle_id':str(uuid.uuid4()),'control_manifest_sha256':'f'*64}
        self.observation['request_sha256']=interrupt._sha(self.payload)
        self.permission={'issuer':{'owner':{'pid':os.getpid()},'scope':fixture.grant['issuer_scope']}}
        self.prepared=artifact_stage.PreparedControl(self.payload['candidate']['control_bundle_id'],'f'*64,'a'*64,'b'*64)
        self.live=self.enterContext(patch('lifecycle.control.control_permission.require_initial',return_value=self.permission))
        self.prepare=self.enterContext(patch('lifecycle.control.control_activation.prepared_for',return_value=self.prepared))
        self.enterContext(patch('lifecycle.control.artifact_stage.require_prepared'))

    def test_control_stop_keeps_its_own_permission_and_one_use_receipt_without_starting(self):
        with self.assertRaisesRegex(ControlError,'control_operation_recovery_required'):
            interrupt.stop_control_prepared(self.payload,self.observation)
        marker=self.fixture.directory/'control-stop.json'
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(marker.read_text())['request_sha256'],interrupt._sha(self.payload))
        self.assertEqual(self.live.call_count,2)
        self.fixture.systemctl.assert_any_call('stop','streamhome.service',timeout=90)
        self.fixture.systemctl.reset_mock()
        with self.assertRaisesRegex(ControlError,'control_repair_interruption_consumed'):
            interrupt.stop_control_prepared(self.payload,self.observation)
        self.assertFalse(any(call.args[0]=='stop' for call in self.fixture.systemctl.call_args_list))

    def test_permission_loss_after_stop_intent_prevents_os_dispatch(self):
        self.live.side_effect=[self.permission,ControlError('issuer cancelled')]
        with self.assertRaises(ControlError):interrupt.stop_control_prepared(self.payload,self.observation)
        self.assertTrue((self.fixture.directory/'control-stop.json').exists())
        self.assertFalse(any(call.args[0]=='stop' for call in self.fixture.systemctl.call_args_list))

    def test_wrong_prepared_control_cannot_interrupt_the_healthy_service(self):
        self.prepare.return_value=replace(self.prepared,bundle_id=str(uuid.uuid4()))
        with self.assertRaises(ControlError):interrupt.stop_control_prepared(self.payload,self.observation)
        self.fixture.systemctl.assert_not_called()
        self.assertFalse((self.fixture.directory/'control-stop.json').exists())


if __name__ == '__main__':unittest.main()
