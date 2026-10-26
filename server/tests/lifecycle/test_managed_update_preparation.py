"""Actual canonical lease/journal writes, with only OS/build boundaries substituted."""
from dataclasses import replace
import json
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller, update_execution
from lifecycle.layout import layout_for_root
from lifecycle.runtime_build import PreparedRuntime
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_update_execution import journal


class ManagedUpdatePreparationFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'application'
        self.root.mkdir(mode=0o700)
        (self.root / 'server').mkdir()
        for name in ('start.sh', 'update.sh'):
            (self.root / name).write_text('#!/usr/bin/env bash\nexit 0\n')
        paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        claim = state.claim_operation_with_credential(self.root,
            operation_type=state.OperationType.UPDATE, owner_kind=state.OwnerKind.UPDATER)
        self.operation = claim.state.active_operation.operation_id
        payload = journal();payload.update(transaction_id=self.operation, managed_candidate=None)
        payload['managed_execution']['operation_id'] = self.operation
        payload['managed_execution']['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        payload['managed_execution']['supervisor_pid'] = os.getpid()
        payload['managed_execution']['supervisor_start_identity'] = state._process_start_identity(os.getpid())
        self.path, _, _ = update_controller._update_paths(self.root, self.operation, 1)
        self.path.write_text(json.dumps(payload));self.path.chmod(0o600)
        self.lease, transfer = state.acquire_transferred_controller_lease(self.root,
            operation_id=self.operation, generation=1, expected_phase=state.LifecyclePhase.RESERVED,
            owner_capability=claim.owner_capability, next_owner_kind=state.OwnerKind.UPDATER)
        self.addCleanup(lambda: self.lease.release() if not self.lease._released else None)
        update_controller._record_transferred_generation(self.root, operation_id=self.operation,
            previous_generation=1, generation=2, lifecycle_phase=state.LifecyclePhase.RESERVED,
            target_commit=payload['target_commit'], previous_commit=payload['previous_commit'])
        self.parent = update_execution.parse(update_controller.read_update_journal(self.path)).parent
        layout = layout_for_root(self.root)
        descriptor = SimpleNamespace(service_uid=os.geteuid(), installation_id=self.parent.installation_id)
        self.layout = replace(layout, descriptor=SimpleNamespace(descriptor=descriptor),
            cache=Path(self.temporary.name)/'cache')
        self.enterContext(patch.object(managed_update, 'layout_for_root', return_value=self.layout))
        self.enterContext(patch('lifecycle.control.tickets.read_stop_epoch', return_value=self.parent.stop_epoch))
        def original_controller(root, lease):
            lease.require_current()
            return managed_update.bound_journal(root, self.operation)
        self.original_permission = managed_update.require_initial_controller
        self.require = self.enterContext(patch.object(managed_update, 'require_initial_controller',
            side_effect=original_controller))
        # Existing source snapshot and runtime inventory tests exercise these
        # independent boundaries with real Git inputs and real manifests.
        self.source = self.layout.cache/'update-preparation'/self.operation/'checkout'
        self.enterContext(patch.object(managed_update, 'preparation_source', return_value=self.source))
        candidate = update_execution.parse(journal()).candidate
        self.built = PreparedRuntime(candidate.runtime_bundle_id, candidate.python_environment_id,
            candidate.node_environment_id, candidate.web_bundle_id,
            candidate.runtime_manifest_sha256, candidate.web_asset_manifest_sha256)
        self.verify = self.enterContext(patch.object(managed_update, 'verify_runtime'))


class ManagedUpdatePreparationTests(ManagedUpdatePreparationFixture, unittest.TestCase):
    def test_exact_target_checkout_preserves_installed_head_and_retains_attempt_on_repeat(self):
        self.layout.cache.mkdir(mode=0o700)
        names = ('server/main.py', 'server/requirements.txt', 'server/requirements.lock',
            'web/package.json', 'web/package-lock.json')
        for name in names:
            file = self.root/name;file.parent.mkdir(exist_ok=True)
            file.write_text('installed committed input')
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(self.root), *args],
                stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q');git('add', 'server', 'web', 'start.sh', 'update.sh')
        git('-c', 'user.name=Update test', '-c', 'user.email=update@example.invalid',
            'commit', '-qm', 'test: installed source')
        previous = git('rev-parse', 'HEAD')
        (self.root/'server/main.py').write_text('candidate committed input')
        git('add', 'server/main.py')
        git('-c', 'user.name=Update test', '-c', 'user.email=update@example.invalid',
            'commit', '-qm', 'test: candidate source')
        target = git('rev-parse', 'HEAD');git('checkout', '--detach', previous)
        payload = update_controller.read_update_journal(self.path)
        payload.update(previous_commit=previous, current_commit=previous, target_commit=target)
        payload['managed_execution']['execution']['application_commit'] = previous
        self.path.write_text(json.dumps(payload))
        self.parent = update_execution.parse(payload).parent
        installed_status = git('status', '--porcelain');saved = self.path.read_bytes()
        source = managed_update.prepare_checkout(self.root, self.lease)
        self.assertEqual((source/'server/main.py').read_text(), 'candidate committed input')
        self.assertEqual((self.root/'server/main.py').read_text(), 'installed committed input')
        self.assertEqual(git('rev-parse', 'HEAD'), previous)
        self.assertEqual(git('status', '--porcelain'), installed_status)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(source.stat().st_mode & 0o777, 0o700)
        for log in source.parent.glob('*.log'):
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        snapshot = {str(p.relative_to(source.parent)): p.read_bytes()
            for p in source.parent.rglob('*') if p.is_file()}
        with self.assertRaises(FileExistsError):managed_update.prepare_checkout(self.root, self.lease)
        self.assertEqual({str(p.relative_to(source.parent)): p.read_bytes()
            for p in source.parent.rglob('*') if p.is_file()}, snapshot)

    def test_initial_preflight_requires_exact_live_supervisor_controller_and_stop_epoch(self):
        active = SimpleNamespace(installation_id=self.parent.installation_id, execution=self.parent.execution)
        supervisor = {'ActiveState': 'active', 'MainPID': str(os.getpid()),
            'InvocationID': self.parent.supervisor_invocation_id}
        actor = {**supervisor, 'InvocationID': '2'*32}
        with patch.object(managed_update, 'read_active_execution', return_value=active), patch(
                'lifecycle.control.processes.controller_peer'), patch(
                'lifecycle.control.administration.operation_unit', return_value=actor), patch(
                'lifecycle.control.privileged.unit_snapshot', return_value=supervisor), patch.dict(
                os.environ, {'INVOCATION_ID': '2'*32}):
            self.original_permission(self.root, self.lease)
            for key, invalid in (('MainPID', '1'), ('InvocationID', 'f'*32), ('ActiveState', 'inactive')):
                original = supervisor[key];supervisor[key] = invalid
                with self.subTest(supervisor_field=key), self.assertRaises(ControlError):
                    self.original_permission(self.root, self.lease)
                supervisor[key] = original
            actor['MainPID'] = '1'
            with self.assertRaises(ControlError):self.original_permission(self.root, self.lease)
            actor['MainPID'] = str(os.getpid())
            active.installation_id = 'foreign'
            with self.assertRaises(ControlError):self.original_permission(self.root, self.lease)
            active.installation_id = self.parent.installation_id
            with patch('lifecycle.control.tickets.read_stop_epoch', return_value='f'*64):
                with self.assertRaises(ControlError):self.original_permission(self.root, self.lease)

    def test_new_tuple_is_written_once_in_original_journal_without_claim_or_acceptance(self):
        original = update_controller.read_update_journal(self.path)
        authority = state.inspect_state(self.root)
        def build(root, **kwargs):
            self.assertEqual(root, self.root)
            self.assertEqual(kwargs['source_root'], self.source)
            self.assertEqual(kwargs['application_commit'], original['target_commit'])
            kwargs['checkpoint']()
            return self.built
        with patch.object(managed_update, 'prepare_runtime', side_effect=build) as prepare:
            candidate = managed_update.prepare_candidate(self.root, self.lease)
            parsed = update_execution.parse(update_controller.read_update_journal(self.path))
            self.assertEqual(parsed.parent, self.parent)
            self.assertEqual(parsed.candidate, candidate)
            self.assertIsNone(parsed.accepted)
            self.assertEqual(state.inspect_state(self.root), authority)
            self.verify.assert_called_once_with(self.root, candidate, source_root=self.source)
            with self.assertRaisesRegex(ControlError, 'control_update_candidate_already_bound'):
                managed_update.prepare_candidate(self.root, self.lease)
            self.assertEqual(prepare.call_count, 1)

    def test_changed_original_journal_during_build_cannot_publish_prepared_candidate(self):
        def build(root, **kwargs):
            update_controller.mutate_update_journal(self.root, operation_id=self.operation,
                generation=2, expected_lifecycle_phase='reserved', changes={'message': 'Concurrent owner change.'})
            return self.built
        with patch.object(managed_update, 'prepare_runtime', side_effect=build):
            with self.assertRaises(ControlError):managed_update.prepare_candidate(self.root, self.lease)
        self.assertIsNone(update_execution.parse(update_controller.read_update_journal(self.path)).candidate)
        self.assertEqual(state.inspect_state(self.root).generation, 2)

    def test_cancellation_during_build_retains_original_journal_and_does_not_verify_or_publish(self):
        original = self.path.read_bytes()
        def build(root, **kwargs):
            self.lease.release()
            kwargs['checkpoint']()
            return self.built
        with patch.object(managed_update, 'prepare_runtime', side_effect=build):
            with self.assertRaises(state.LifecycleError):managed_update.prepare_candidate(self.root, self.lease)
        self.assertEqual(self.path.read_bytes(), original)
        self.verify.assert_not_called()


if __name__ == '__main__':unittest.main()
