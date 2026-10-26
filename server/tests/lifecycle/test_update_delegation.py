"""Real owner lock/lease, explicit substituted kernel records; no native claim."""
import json
import io
from contextlib import redirect_stdout
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller, update_history
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from tests.lifecycle.test_managed_update_preparation import ManagedUpdatePreparationFixture


class UpdateDelegateFixture(ManagedUpdatePreparationFixture):
    def setUp(self):
        super().setUp()
        main = os.getpid();helper = main+100000;shell = main+100001
        group = '/system.slice/streamhome-operation@'+self.operation+'.service'
        def process(pid, parent, start, arguments, executable):
            return {'pid': pid, 'parent': parent, 'start': start, 'status': 'S',
                'uid': os.geteuid(), 'cgroup': group, 'arguments': arguments, 'executable': executable}
        root = self.root
        self.records = {
            main: process(main, 1, self.lease.lease.process_start_identity,
                [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision',
                    b'--operation', self.operation.encode(), b''], os.path.realpath('/usr/bin/python3')),
            shell: process(shell, main, '12345', [b'bash',
                os.fsencode(self.path.parent/('update-controller.'+self.operation+'.sh')),
                b'--manual-execute', b'e'*40, b'a'*40, b'main', b'true', os.fsencode(root), b''], '/usr/bin/bash'),
            helper: process(helper, shell, '12346', [], os.path.realpath('/usr/bin/python3'))}
        self.main, self.shell, self.helper = main, shell, helper
        payload = update_controller.read_update_journal(self.path)
        payload.update(controller_pid=shell, controller_start_ticks='12345')
        self.path.write_text(json.dumps(payload))
        self.unit = {'ActiveState': 'active', 'MainPID': str(main), 'InvocationID': '2'*32}
        self.process_reader = managed_update._process_record
        self.enterContext(patch.object(managed_update, '_process_record', side_effect=lambda pid: self.records[pid]))
        self.enterContext(patch('lifecycle.control.administration.operation_unit', return_value=self.unit))
        self.enterContext(patch.dict(os.environ, {'INVOCATION_ID': '2'*32}))
        self.enterContext(patch.object(managed_update.os, 'getpid', return_value=helper))

    def proof(self):return managed_update.delegated_controller(self.root, self.operation, 2)


class UpdateDelegateTests(UpdateDelegateFixture, unittest.TestCase):
    def test_quiescent_delegate_names_only_the_registered_shell_and_fixed_runtime_worker(self):
        self.records[self.helper]['arguments'] = [b'/usr/bin/python3', b'-I',
            b'/usr/local/libexec/streamhome/provision', b'--worker', b'update-runtime',
            self.operation.encode(), b'']
        before = state.inspect_state(self.root), self.path.read_bytes()
        reference = managed_update.quiescent_delegate(self.root, self.lease.lease, helper_pid=self.helper)
        self.assertEqual(reference, (self.shell, '12345', self.helper, '12346'))
        from pathlib import Path
        original_read = Path.read_text
        children = [str(self.helper)]
        def read(path, *args, **kwargs):
            if str(path) == f'/proc/{self.shell}/task/{self.shell}/children':
                return children[0]
            return original_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.assertEqual(managed_update.quiescent_delegate(self.root, self.lease.lease), reference)
            extra = self.helper+10
            self.records[extra] = {**self.records[self.helper], 'pid': extra, 'start': '12399'}
            children[0] += ' '+str(extra)
            with self.assertRaises(ControlError):
                managed_update.quiescent_delegate(self.root, self.lease.lease)
        self.records[self.helper]['arguments'] = [b'/usr/bin/python3', b'-c', b'unknown']
        with self.assertRaises(ControlError):
            managed_update.quiescent_delegate(self.root, self.lease.lease, helper_pid=self.helper)
        self.assertEqual((state.inspect_state(self.root), self.path.read_bytes()), before)

    def test_exact_child_chain_can_retain_terminal_evidence_without_acquiring_or_releasing_owner(self):
        proof = self.proof();proof.require_current()
        self.assertFalse(hasattr(proof, 'release'))
        payload = update_controller.read_update_journal(self.path);payload['phase'] = 'failed'
        self.path.write_text(json.dumps(payload))
        active = SimpleNamespace(installation_id=self.parent.installation_id,
            execution=self.parent.execution, operation_id=self.operation, generation=0)
        with patch.object(update_history, 'read_active_execution', return_value=active):
            retained = update_history.retain_terminal(self.root, proof)
        self.assertEqual(retained['journal'], payload)
        self.assertFalse(self.lease._released)

    def test_protected_worker_pins_reader_and_prints_only_retention_receipt(self):
        from lifecycle.control import workers
        payload = update_controller.read_update_journal(self.path);payload['phase'] = 'failed'
        self.path.write_text(json.dumps(payload))
        active = SimpleNamespace(installation_id=self.parent.installation_id,
            execution=self.parent.execution, operation_id=self.operation, generation=0)
        source = SimpleNamespace(buffer=io.BytesIO(b'{"schema_version":1,"generation":2}'))
        stdout = io.StringIO()
        with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'inspect_installation',
                return_value=self.layout.descriptor), patch.object(workers.sys, 'stdin', source), patch.object(
                update_history, 'read_active_execution', return_value=active), redirect_stdout(stdout):
            self.assertEqual(workers.main(['retain-update', self.operation], bundle=self.root, manifest={},
                manifest_sha256=self.parent.execution.control_manifest_sha256), 0)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(set(receipt), {'retained', 'generation', 'journal_sha256'})
        self.assertTrue(receipt['retained'])
        self.assertEqual(receipt['generation'], 2)
        source.buffer.seek(0)
        with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'inspect_installation',
                return_value=self.layout.descriptor), patch.object(workers.sys, 'stdin', source):
            with self.assertRaisesRegex(ControlError, 'control_parent_binding_changed'):
                workers.main(['retain-update', self.operation], bundle=self.root, manifest={}, manifest_sha256='f'*64)

    def accepted_completion(self, previous):
        for old, new in ((state.LifecyclePhase.RESERVED, state.LifecyclePhase.EXCLUSIVE),
                (state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING)):
            state.transition_operation_with_credential(self.root, operation_id=self.operation,
                generation=2, expected_phase=old, next_phase=new)
        payload = update_controller.read_update_journal(self.path)
        candidate = managed_update.prepared_execution(self.parent.execution, payload['target_commit'], self.built)
        selected = self.parent.execution if previous else candidate
        payload.update(phase='recovering', lifecycle_phase='verifying', runtime_committed=True,
            runtime_committed_at=1, current_commit=selected.application_commit,
            managed_candidate=execution_tuple_payload(candidate),
            managed_verification_execution_sha256=execution_tuple_sha256(selected),
                managed_accepted_execution_sha256=execution_tuple_sha256(selected))
        self.path.write_text(json.dumps(payload))
        active = SimpleNamespace(installation_id=self.parent.installation_id, execution=selected,
            operation_id=self.operation, generation=2)
        with patch.object(update_history, 'read_active_execution', return_value=active), patch.object(
                managed_update, 'runtime_action', return_value={'ready': True}):
            record = managed_update.retain_completed_from_delegate(self.root, self.proof())
        self.assertEqual(record['journal']['phase'], 'rolled_back' if previous else 'succeeded')
        self.assertEqual(record['journal']['managed_accepted_execution_sha256'], execution_tuple_sha256(selected))
        self.assertTrue(record['journal']['runtime_committed'])
        self.assertEqual(record['journal']['runtime_committed_at'], 1)
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(state.inspect_state(self.root).generation, 2)
        self.assertFalse(self.lease._released)

    def test_accepted_target_status_is_terminal_before_retention_without_completing_authority(self):
        self.accepted_completion(False)

    def test_accepted_previous_status_is_terminal_before_retention_without_revoking_acceptance(self):
        self.accepted_completion(True)

    def test_reused_foreign_moved_or_reparented_processes_never_inherit_owner_permission(self):
        cases = ((self.helper, 'uid', os.geteuid()+1), (self.helper, 'cgroup', '/foreign'),
            (self.shell, 'parent', 1), (self.shell, 'status', 'Z'),
            (self.shell, 'executable', '/untrusted/bash'), (self.main, 'start', 'reused'),
            (self.main, 'cgroup', '/foreign'))
        for pid, field, invalid in cases:
            old = self.records[pid][field];self.records[pid][field] = invalid
            with self.subTest(pid_role=pid-self.main, field=field), self.assertRaises(ControlError):self.proof()
            self.records[pid][field] = old
        old = self.records[self.shell]['arguments']
        self.records[self.shell]['arguments'] = [*old[:-2], b'/different/installation', b'']
        with self.assertRaises(ControlError):self.proof()
        self.records[self.shell]['arguments'] = old
        self.unit['MainPID'] = str(self.shell)
        with self.assertRaises(ControlError):self.proof()

    def test_kernel_record_reader_rechecks_start_identity_without_granting_authority(self):
        observed = self.process_reader(self.main)
        self.assertEqual(observed['pid'], self.main)
        self.assertEqual(observed['uid'], os.geteuid())
        self.assertEqual(observed['start'], self.lease.lease.process_start_identity)
        self.assertEqual(observed['executable'], os.path.realpath('/usr/bin/python3'))
        with patch('lifecycle.control.administration_lock.process_identity',
                side_effect=[(observed['start'], 'S'), ('reused', 'S')]):
            with self.assertRaisesRegex(ControlError, 'control_process_changed'):
                self.process_reader(self.main)

    def test_owner_projection_requires_exact_current_operation_main_process(self):
        observed = managed_update.require_owner_peer(self.root, self.lease.lease, self.main)
        self.assertEqual(observed[0].generation, 2)
        self.assertEqual(observed[2].parent, self.parent)
        with self.assertRaises(ControlError):
            managed_update.require_owner_peer(self.root, self.lease.lease, self.helper)
        self.unit['MainPID'] = str(self.shell)
        with self.assertRaises(ControlError):
            managed_update.require_owner_peer(self.root, self.lease.lease, self.main)

    def test_owner_and_helper_cannot_substitute_a_different_interpreter(self):
        for pid in (self.main, self.helper):
            old = self.records[pid]['executable']
            self.records[pid]['executable'] = '/untrusted/python'
            with self.subTest(role=pid-self.main), self.assertRaises(ControlError):self.proof()
            self.records[pid]['executable'] = old

    def test_claimed_shell_pid_or_request_cannot_replace_the_observed_kernel_parent(self):
        for change in ({'controller_pid': self.main}, {'controller_start_ticks': 'reused'}):
            payload = update_controller.read_update_journal(self.path)
            original = self.path.read_bytes();payload.update(change)
            self.path.write_text(json.dumps(payload))
            with self.subTest(change=change), self.assertRaises(ControlError):self.proof()
            self.path.write_bytes(original)
        original = self.records[self.main]['arguments']
        self.records[self.main]['arguments'] = [b'python3', b'untrusted-owner.py', b'']
        with self.assertRaises(ControlError):self.proof()
        self.records[self.main]['arguments'] = original


if __name__ == '__main__':unittest.main()
