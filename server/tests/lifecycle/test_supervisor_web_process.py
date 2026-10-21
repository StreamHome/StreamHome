import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.request
import uuid

from lifecycle.control.supervisor import web_command
from lifecycle.control import supervisor as managed
from lifecycle.control.rpc import ControlError
from lifecycle.control import tickets
from lifecycle import management_journal as contract, state
from lifecycle.control.requests import parse_management_request
from lifecycle.deployment_config import default_deployment
from tests.lifecycle.test_management_requests import request_payload


class SupervisorWebProcessTests(unittest.TestCase):
    def test_stop_between_backend_and_web_launch_prevents_second_process(self):
        instance = managed.Supervisor.__new__(managed.Supervisor)
        instance.launches_open = True
        instance.children = {}
        instance.layout = SimpleNamespace(runtime=Path('/run/streamhome'))
        process = SimpleNamespace(pid=123)

        def launch_backend(*arguments, **keywords):
            instance.launches_open = False
            return process

        with patch.object(managed.subprocess, 'Popen', side_effect=launch_backend) as spawn, patch.object(state, '_process_start_identity', return_value='42'), patch.object(state, '_atomic_write_json'):
            instance._spawn('backend', ['/usr/bin/python3'], {}, Path('/'))
            with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
                instance._spawn('web', ['/usr/bin/node'], {}, Path('/'))
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(set(instance.children), {'backend'})

    def test_changed_operator_stop_epoch_rejects_live_controller(self):
        reviewed=parse_management_request(request_payload())
        generation=reviewed.expected_generation+1
        journal=contract.ManagementJournal(reviewed.installation_id,reviewed.operation_id,generation,reviewed.action,reviewed.activation_mode,
            reviewed.review_id,reviewed.previous,reviewed.candidate,'running','1'*32,reviewed.supervisor_invocation_id,contract.ManagementStage.VERIFYING,None)
        authority=state.LifecycleState(2,generation,state.ActiveOperation(reviewed.operation_id,state.OperationType.MANAGEMENT,state.LifecyclePhase.VERIFYING,state.OwnerKind.MANAGEMENT_CONTROLLER,'c'*64))
        lease=SimpleNamespace(pid=200,operation_id=reviewed.operation_id,generation=generation)
        instance=managed.Supervisor.__new__(managed.Supervisor)
        instance.installation=reviewed.installation_id
        instance.invocation=reviewed.supervisor_invocation_id
        with patch.object(managed,'controller_peer'), patch.object(state,'read_controller_lease',return_value=lease), patch.object(state,'inspect_state',return_value=authority), patch.object(state,'controller_lease_is_current',return_value=True), patch.object(managed.management_store,'inspect_management_journal',return_value=journal), patch.object(managed,'read_management_request',return_value=reviewed), patch.object(managed,'read_stop_epoch',return_value='f'*64):
            with self.assertRaises(ControlError) as error:
                instance._controller(managed.Peer(200,os.geteuid(),os.getegid()),{'operation_id':reviewed.operation_id,'generation':generation},phase=state.LifecyclePhase.VERIFYING)
        self.assertEqual(error.exception.code,'control_operator_stopped')

    def test_first_start_ticket_is_invalidated_by_new_operator_stop(self):
        reviewed=parse_management_request(request_payload())
        layout=SimpleNamespace(descriptor=SimpleNamespace(descriptor=SimpleNamespace(installation_id=reviewed.installation_id)))
        value={'schema_version':1,'action':'first_start','installation_id':reviewed.installation_id,'operation_id':reviewed.operation_id,
            'execution_sha256':contract.execution_tuple_sha256(reviewed.previous),'invocation':'1'*32,
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'stop_epoch':'b'*64,
            'expires_at_monotonic':time.monotonic()+100,'nonce':'c'*64}
        with patch.object(tickets,'read_protected_json',return_value=value), patch.object(tickets,'read_stop_epoch',return_value='b'*64):
            self.assertEqual(tickets.require_first_start(layout,reviewed.previous,'1'*32),value)
        with patch.object(tickets,'read_protected_json',return_value=value), patch.object(tickets,'read_stop_epoch',return_value='d'*64), self.assertRaises(ControlError):
            tickets.require_first_start(layout,reviewed.previous,'1'*32)

    def test_stop_signal_closes_gate_while_event_loop_is_synchronously_blocked(self):
        script='''import asyncio,os,signal,threading,time
from lifecycle.control.supervisor import immediate_stop_signals
async def main():
 for selected in (signal.SIGTERM,signal.SIGINT):
  gate={'open':True}
  previous={value:signal.getsignal(value) for value in (signal.SIGTERM,signal.SIGINT)}
  def close():gate['open']=False
  def send():
   time.sleep(0.05)
   os.kill(os.getpid(),selected)
  with immediate_stop_signals(close):
   sender=threading.Thread(target=send)
   sender.start()
   time.sleep(0.2)
   assert gate['open'] is False,'Stop was deferred until the event loop resumed'
   sender.join()
  assert all(signal.getsignal(value)==handler for value,handler in previous.items())
asyncio.run(main())
'''
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_http_listener_pid_is_the_exact_owned_launch_leader(self):
        node = shutil.which('node')
        modules = Path(__file__).resolve().parents[3] / 'web/node_modules'
        if not node or not (modules/'tsx/dist/loader.mjs').is_file():
            self.skipTest('The existing Node/tsx test dependencies are unavailable.')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'web').mkdir()
            identity = str(uuid.uuid4())
            bundle = root/'runtime/web'/identity
            bundle.mkdir(parents=True)
            (bundle/'node_modules').symlink_to(modules, target_is_directory=True)
            (root/'web/server.ts').write_text("import {createServer} from 'node:http'; import type {AddressInfo} from 'node:net'; const server=createServer((_req,res)=>res.end(JSON.stringify({pid:process.pid})));server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({pid:process.pid,port:(server.address() as AddressInfo).port})));\n")
            execution = SimpleNamespace(web_bundle_id=identity, node_executable=node)
            environment = {'PATH':str(Path(node).parent)+':/usr/bin:/bin','HOME':str(root),'TMPDIR':str(root),'TSX_DISABLE_CACHE':'1','LC_ALL':'C.UTF-8'}
            with (root/'node.stderr').open('wb') as errors:
                process = subprocess.Popen(web_command(root, execution), cwd=root/'web', env=environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(10), 'Node did not publish its listener within the startup bound.')
                    raw = process.stdout.readline(8192)
                    self.assertTrue(raw, (root/'node.stderr').read_text())
                    reported = json.loads(raw)
                    self.assertEqual(reported['pid'], process.pid)
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open('http://127.0.0.1:'+str(reported['port']), timeout=2) as response:
                        self.assertEqual(json.loads(response.read(4096)), {'pid':process.pid})
                finally:
                    # The leader remains unreaped until its owned group is
                    # stopped, including the disposable esbuild helper.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    process.stdout.close()


class SupervisorLaunchAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_quarantine_preserves_idle_active_runtime_without_bypassing_unresolved_authority(self):
        execution = parse_management_request(request_payload()).previous
        instance = managed.Supervisor.__new__(managed.Supervisor)
        instance.installation = str(uuid.uuid4())
        instance.invocation = '1' * 32
        instance.sequence = 0
        instance.control_bundle_id = execution.control_bundle_id
        instance.control_manifest_sha256 = execution.control_manifest_sha256
        instance.launches_open = True
        instance.children = {}
        instance._spawn = Mock()
        instance._deployment = Mock(return_value=default_deployment())
        desired = str(uuid.uuid4())
        manifest = SimpleNamespace(active=SimpleNamespace(revision=execution.deployment_revision),
            desired=SimpleNamespace(revision=desired), quarantined=(desired,))
        descriptor = SimpleNamespace(installation_id=instance.installation, service_uid=os.geteuid(), status=managed.InstallationStatus.READY)
        with tempfile.TemporaryDirectory() as directory:
            layout = SimpleNamespace(descriptor=SimpleNamespace(descriptor=descriptor), domains=Path(directory),
                state=Path('/var/lib/streamhome'), runtime=Path('/run/streamhome'))
            with patch.object(managed, 'runtime_children', return_value=set()), patch.object(managed, 'layout_for_root', return_value=layout), patch.object(managed, 'verify_runtime'), patch.object(managed, 'verify_links'), patch.object(state, 'inspect_state', return_value=SimpleNamespace(active_operation=None)), patch.object(managed, 'ConfigurationStore') as store:
                store.return_value.__enter__.return_value.inspect.return_value = manifest
                await instance.launch(execution)
            self.assertEqual(instance._spawn.call_count, 2)
            instance._spawn.reset_mock()
            with patch.object(managed, 'runtime_children', return_value=set()), patch.object(managed, 'layout_for_root', return_value=layout), patch.object(managed, 'verify_runtime'), patch.object(managed, 'verify_links'), patch.object(state, 'inspect_state', return_value=SimpleNamespace(active_operation=object())):
                with self.assertRaisesRegex(ControlError, 'control_recovery_required'):
                    await instance.launch(execution)
            instance._spawn.assert_not_called()

    async def test_controller_is_revalidated_after_verification_before_any_child_spawn(self):
        reviewed=parse_management_request(request_payload())
        journal=contract.ManagementJournal(reviewed.installation_id,reviewed.operation_id,reviewed.expected_generation+1,reviewed.action,reviewed.activation_mode,
            reviewed.review_id,reviewed.previous,reviewed.candidate,'running','1'*32,reviewed.supervisor_invocation_id,contract.ManagementStage.VERIFYING,None)
        instance=managed.Supervisor.__new__(managed.Supervisor)
        instance.installation=reviewed.installation_id
        instance.control_bundle_id=reviewed.candidate.control_bundle_id
        instance.control_manifest_sha256=reviewed.candidate.control_manifest_sha256
        instance.invocation=reviewed.supervisor_invocation_id
        instance.launches_open=True
        instance.children={}
        instance._spawn=Mock()
        instance._deployment=Mock(return_value=default_deployment())
        descriptor=SimpleNamespace(installation_id=instance.installation,service_uid=os.geteuid(),status=managed.InstallationStatus.READY)
        layout=SimpleNamespace(descriptor=SimpleNamespace(descriptor=descriptor),state=Path('/var/lib/streamhome'),runtime=Path('/run/streamhome'))
        authorize=Mock(side_effect=ControlError('control_controller_changed'))
        with patch.object(managed,'runtime_children',return_value=set()), patch.object(managed,'layout_for_root',return_value=layout), patch.object(managed,'verify_runtime'), patch.object(managed,'verify_links'), patch.object(state,'inspect_state',return_value=SimpleNamespace(active_operation=object())):
            with self.assertRaises(ControlError) as error:
                await instance.launch(reviewed.candidate,journal=journal,authorize=authorize)
        self.assertEqual(error.exception.code,'control_controller_changed')
        authorize.assert_called_once_with()
        instance._spawn.assert_not_called()

    async def test_stop_during_verification_prevents_backend_and_web_spawn(self):
        instance=managed.Supervisor.__new__(managed.Supervisor)
        instance.installation=str(uuid.uuid4())
        instance.control_bundle_id=str(uuid.uuid4())
        instance.control_manifest_sha256='a'*64
        instance.launches_open=True
        instance.children={}
        instance._spawn=Mock()
        execution=SimpleNamespace(control_bundle_id=instance.control_bundle_id,control_manifest_sha256=instance.control_manifest_sha256,control_protocol_version=1)
        descriptor=SimpleNamespace(installation_id=instance.installation,service_uid=os.geteuid(),status=managed.InstallationStatus.READY)
        layout=SimpleNamespace(descriptor=SimpleNamespace(descriptor=descriptor))
        def stop_during_verify(*arguments):instance.launches_open=False
        with patch.object(managed,'runtime_children',return_value=set()), patch.object(managed,'layout_for_root',return_value=layout), patch.object(managed,'verify_runtime',side_effect=stop_during_verify), patch.object(managed,'verify_links'):
            with self.assertRaises(ControlError) as error:
                await instance.launch(execution)
        self.assertEqual(error.exception.code,'control_operator_stopped')
        instance._spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
