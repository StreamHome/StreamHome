import asyncio
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import backend_control, coordination, management_journal as contract, state
from lifecycle.control import repair, runtime_preparation as preparation
from lifecycle.control.rpc import ControlError, Peer
from lifecycle.control.requests import parse_management_request
from lifecycle.terminal import Console
from tests.lifecycle.test_management_requests import request_payload


def grant():
    reviewed = parse_management_request(request_payload())
    return {'schema_version': 1, 'installation_id': reviewed.installation_id, 'preparation_id': str(uuid.uuid4()),
        'expected_generation': reviewed.expected_generation, 'previous': contract.execution_tuple_payload(reviewed.previous),
        'expected_desired': reviewed.expected_desired.payload(), 'supervisor': reviewed.supervisor.payload(),
        'backend': reviewed.backend.payload(), 'supervisor_invocation_id': reviewed.supervisor_invocation_id,
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': 'a' * 64,
        'control_manifest_sha256': reviewed.controller_control_manifest_sha256, 'expires_at_monotonic': time.monotonic() + 300}


def replacement(previous):
    bundle, python, node, web = (str(uuid.uuid4()) for _ in range(4))
    base = Path('/opt/streamhome/runtime')
    return replace(previous, runtime_bundle_id=bundle, runtime_manifest_sha256='f' * 64,
        python_environment_id=python, python_executable=str(base / 'python' / python / 'venv/bin/python'),
        node_environment_id=node, node_executable=str(base / 'node' / node / 'bin/node'),
        web_bundle_id=web, web_dependency_target=str(base / 'web' / web / 'node_modules'),
        web_dist_target=str(base / 'web' / web / 'dist'))


class RuntimePreparationContractTests(unittest.TestCase):
    def test_grant_has_only_cache_inputs_and_no_placeholder_candidate_or_launch_permission(self):
        value = grant()
        self.assertEqual(preparation.parse_grant(deepcopy(value)), value)
        for change in ({'candidate': value['previous']}, {'launch': True}, {'expected_generation': True},
                       {'backend': None}, {'supervisor_invocation_id': '0' * 32}, {'expires_at_monotonic': float('nan')},
                       {'preparation_id': '../../another-unit'}, {'stop_epoch': 'invalid'}):
            with self.subTest(change=change), self.assertRaises((ControlError, ValueError)):
                preparation.parse_grant({**value, **change})

    def test_expired_or_stopped_grant_cannot_reach_authority_or_build(self):
        value = grant()
        selected = SimpleNamespace(service_uid=os.geteuid(), status=preparation.InstallationStatus.READY, installation_id=value['installation_id'])
        layout = SimpleNamespace(descriptor=SimpleNamespace(descriptor=selected))
        for changed in ({'expires_at_monotonic': 0.1}, {'stop_epoch': 'b' * 64}):
            current = {**value, **changed}
            with self.subTest(changed=changed), patch.object(preparation, 'layout_for_root', return_value=layout), patch.object(preparation, 'read_grant', return_value=current), patch.object(preparation, 'read_stop_epoch', return_value='a' * 64), patch.object(state, 'inspect_state') as inspect:
                with self.assertRaisesRegex(ControlError, 'control_preparation_changed'):
                    preparation.require_current(current)
                inspect.assert_not_called()

    def test_repair_result_cannot_change_application_deployment_assets_or_provider_revisions(self):
        value = grant()
        previous = replacement(contract.parse_execution_tuple(value['previous']))
        layout = SimpleNamespace(cache=Path('/unused-private-cache'))
        for candidate in (replace(previous, application_commit='e' * 40), replace(previous, deployment_revision=str(uuid.uuid4())),
                          replace(previous, web_asset_manifest_sha256='f' * 64), replace(previous, provider_revisions=(('cloudflare', 4),))):
            payload = {'schema_version': 1, 'grant_sha256': preparation.grant_sha256(value),
                       'execution': contract.execution_tuple_payload(candidate)}
            with self.subTest(candidate=candidate), patch.object(preparation, 'read_grant', return_value=value), patch.object(preparation, 'require_current'), patch.object(preparation, 'layout_for_root', return_value=layout), patch.object(preparation, '_private_record', return_value=payload), patch.object(preparation, 'verify_runtime') as verify:
                with self.assertRaises(ValueError):
                    preparation.result(value['preparation_id'])
                verify.assert_not_called()

    def test_root_repair_module_imports_no_canonical_or_application_implementation(self):
        code = "import sys;import lifecycle.control.repair;assert not {'lifecycle.state','lifecycle.management_journal','lifecycle.configuration_store','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_root_repair_requires_resolved_authority_and_a_real_settled_backend_before_grant(self):
        selected = SimpleNamespace(status=SimpleNamespace(value='ready'))
        for active, process in (({'operation_id': str(uuid.uuid4())}, None), (None, {'initial_service_state': 'running', 'backend': None})):
            snapshot = {'lifecycle': {'active_operation': active}}
            with self.subTest(active=active), patch.object(repair, 'require_root'), patch.object(repair, 'descriptor', return_value=selected), patch.object(repair, 'root_stop_epoch', return_value='a' * 64), patch.object(repair, 'run_worker', return_value=snapshot), patch.object(repair, 'process_snapshot', return_value=process), patch.object(repair, '_prepare') as build, patch.object(repair, 'open_console') as console:
                with self.assertRaisesRegex(ControlError, 'control_operation_recovery_required' if active else 'control_interrupting_repair_required'):
                    repair.repair(request_path=None, control_manifest_sha256='a' * 64)
                build.assert_not_called()
                console.assert_not_called()

    def test_declining_preparation_cannot_publish_a_grant_or_claim_management(self):
        value = grant()
        snapshot = {'lifecycle': {'active_operation': None}, 'execution': {'execution': value['previous']}}
        processes = {'initial_service_state': 'running', 'backend': value['backend']}
        selected = SimpleNamespace(status=SimpleNamespace(value='ready'))
        console = Console(io.StringIO('n\n'), io.StringIO(), interactive=True)
        with patch.object(repair, 'require_root'), patch.object(repair, 'descriptor', return_value=selected), patch.object(repair, 'run_worker', return_value=snapshot) as worker, patch.object(repair, 'process_snapshot', return_value=processes), patch.object(repair, '_boot_enabled', return_value=True), patch.object(repair, 'root_stop_epoch', return_value=value['stop_epoch']), patch.object(repair, 'open_console') as open_console, patch.object(repair, '_prepare') as build, patch.object(repair, 'run_operation') as claim:
            open_console.return_value.__enter__.return_value = console
            self.assertEqual(repair.repair(request_path=None, control_manifest_sha256=value['previous']['control_manifest_sha256']), 0)
        self.assertEqual(worker.call_count, 1)
        build.assert_not_called()
        claim.assert_not_called()

    def test_explicit_prepare_only_retains_candidate_without_creating_a_lifecycle_operation(self):
        value = grant()
        snapshot = {'lifecycle': {'active_operation': None, 'generation': value['expected_generation']},
            'execution': {'execution': value['previous']}, 'configuration': {'deployment': {'desired': value['expected_desired']}}}
        selected = SimpleNamespace(status=SimpleNamespace(value='ready'), installation_id=value['installation_id'])
        processes = {name: value[name] for name in ('supervisor', 'backend', 'supervisor_invocation_id')}
        processes['initial_service_state'] = 'running'
        previous = contract.parse_execution_tuple(value['previous'])
        candidate = contract.execution_tuple_payload(replacement(previous))
        explicit = {'schema_version': 1, 'expected_generation': value['expected_generation'],
            'expected_execution_sha256': repair.sha256(value['previous']), 'expected_desired_revision': value['expected_desired']['revision'],
            'prepare': True, 'activate': False, 'interruption_approved': False}
        def worker(arguments, **kwargs):
            if arguments == ['inspect']:
                return snapshot
            self.assertEqual(arguments, ['validate-preparation'])
            return kwargs['payload']
        def build(review):
            return {'schema_version': 1, 'grant_sha256': repair.sha256(review), 'execution': candidate}
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / 'request.json'
            request.write_text(json.dumps(explicit))
            with patch.object(repair, 'require_root'), patch.object(repair, 'descriptor', return_value=selected), patch.object(repair, 'run_worker', side_effect=worker), patch.object(repair, 'process_snapshot', return_value=processes), patch.object(repair, '_boot_enabled', return_value=True), patch.object(repair, 'root_stop_epoch', return_value=value['stop_epoch']), patch.object(repair, '_prepare', side_effect=build), patch.object(repair, 'run_operation') as claim, patch.object(repair, 'open_console') as console, patch.object(repair.sys, 'stdout', io.StringIO()):
                self.assertEqual(repair.repair(request_path=request, control_manifest_sha256=previous.control_manifest_sha256), 0)
        claim.assert_not_called()
        console.assert_not_called()

    def test_stop_while_owner_reviews_repair_cannot_be_adopted_as_fresh_permission(self):
        value = grant()
        snapshot = {'lifecycle': {'active_operation': None}, 'execution': {'execution': value['previous']}}
        processes = {'initial_service_state': 'running', 'backend': value['backend']}
        selected = SimpleNamespace(status=SimpleNamespace(value='ready'))
        console = Console(io.StringIO('\n'), io.StringIO(), interactive=True)
        with patch.object(repair, 'require_root'), patch.object(repair, 'descriptor', return_value=selected), patch.object(repair, 'run_worker', return_value=snapshot) as worker, patch.object(repair, 'process_snapshot', return_value=processes), patch.object(repair, '_boot_enabled', return_value=True), patch.object(repair, 'root_stop_epoch', side_effect=['a' * 64, 'b' * 64]), patch.object(repair, 'open_console') as open_console, patch.object(repair, '_prepare') as build, patch.object(repair, 'run_operation') as claim:
            open_console.return_value.__enter__.return_value = console
            with self.assertRaisesRegex(ControlError, 'control_operator_stopped'):
                repair.repair(request_path=None, control_manifest_sha256=value['previous']['control_manifest_sha256'])
        self.assertEqual(worker.call_count, 1)
        build.assert_not_called()
        claim.assert_not_called()


class CachePreparationCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.instance = backend_control.BackendControl.__new__(backend_control.BackendControl)
        self.instance.cache_preparation = {}
        self.value = grant()
        self.value['backend'] = {'pid': os.getpid(), 'start_identity': state._process_start_identity(os.getpid())}
        self.instance.installation_id = self.value['installation_id']
        self.instance.invocation = self.value['supervisor_invocation_id']
        self.coordinator = coordination.RuntimeCoordinator()
        self.peer = Peer(os.getpid(), os.geteuid(), os.getegid())
        self.arguments = {'preparation_id': self.value['preparation_id'], 'grant_sha256': preparation.grant_sha256(self.value)}
        self.members = {os.getpid()}
        self.instance.preparation_members = Mock(side_effect=lambda _: set(self.members))
        patches = [patch.object(preparation, 'read_grant', return_value=self.value), patch.object(preparation, 'require_current'),
            patch('lifecycle.control.processes.process_cgroup', return_value=f"/system.slice/streamhome-prepare@{self.value['preparation_id']}.service"),
            patch.object(backend_control, 'require_runtime_admission', return_value=SimpleNamespace(admitted=lambda: True)),
            patch.object(backend_control, 'get_runtime_coordinator', return_value=self.coordinator)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addAsyncCleanup(self.stop_watchers)

    async def stop_watchers(self):
        tasks = [record['task'] for record in self.instance.cache_preparation.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_real_coordinator_drain_waits_for_every_preparation_descendant(self):
        self.assertEqual(await self.instance.handle(self.peer, 'register_cache_preparation', self.arguments), {'registered': True})
        fence = self.coordinator.close_admission([coordination.EffectClass.DISPOSABLE_CACHE_WRITE], operation_id=str(uuid.uuid4()), generation=1)
        self.members.add(os.getpid() + 10000)
        with self.assertRaisesRegex(ControlError, 'control_preparation_busy'):
            await self.instance.handle(self.peer, 'release_cache_preparation', self.arguments)
        self.assertFalse((await self.coordinator.drain(fence, timeout=0)).complete)
        self.members = {os.getpid()}
        # Release remains valid even after the root grant became stale. It
        # acknowledges quiescence and cannot authorize further preparation.
        with patch.object(preparation, 'require_current', side_effect=AssertionError('release must not renew authority')):
            self.assertEqual(await self.instance.handle(self.peer, 'release_cache_preparation', self.arguments), {'released': True})
        self.assertTrue((await self.coordinator.drain(fence, timeout=0)).complete)

    async def test_dead_leader_and_reused_pid_never_detach_remaining_cgroup_work(self):
        await self.instance.handle(self.peer, 'register_cache_preparation', self.arguments)
        self.members = {os.getpid() + 10000}
        with patch('lifecycle.control.administration_lock.process_identity', return_value=('0', 'S')):
            await asyncio.sleep(0.03)
            self.assertEqual(len(self.coordinator.active_work()), 1)
            self.members.clear()
            await asyncio.sleep(0.30)
        self.assertEqual(self.coordinator.active_work(), ())

    async def test_another_process_or_changed_grant_cannot_release_owned_work(self):
        await self.instance.handle(self.peer, 'register_cache_preparation', self.arguments)
        for peer, arguments in ((Peer(os.getpid() + 1, os.geteuid(), os.getegid()), self.arguments),
                                (self.peer, {**self.arguments, 'grant_sha256': 'f' * 64})):
            with self.subTest(peer=peer), self.assertRaises(ControlError):
                await self.instance.handle(peer, 'release_cache_preparation', arguments)
        self.assertEqual(len(self.coordinator.active_work()), 1)


if __name__ == '__main__':
    unittest.main()
