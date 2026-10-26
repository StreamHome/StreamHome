"""Actual canonical transfer, Git materialization and child gate; build/OS proof substituted."""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import managed_update, state, update_controller
from lifecycle import update_execution, update_recovery
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from lifecycle.runtime_build import PreparedRuntime
from lifecycle.layout import layout_for_root
from tests.lifecycle import test_update_controller as controller_fixture
from tests.lifecycle.test_update_execution import journal


class ManagedUpdateEngineHookTests(unittest.TestCase):
    def setUp(self):
        self.fixture = controller_fixture.UpdateControllerTests()
        self.fixture.authority_schema_version = 2;self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root;self.operation = self.fixture.claimed.state.active_operation.operation_id
        source = Path(__file__).resolve().parents[3]/'scripts/runtime/update.sh'
        self.gate = source.read_text().split('ORIGINAL_ROOT=', 1)[0]
        self.marker = self.root/'.run/registered-child.json'
        self.script = self.gate + '''
[[ -z "${STREAMHOME_MANAGED_UPDATE_GATE_FD:-}" ]] || exit 81
/usr/bin/python3 - "${6}/.run/update-state.json" "$BASHPID" "${6}/.run/registered-child.json" <<'INNER'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
pid = int(sys.argv[2])
start = Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')', 1)[1].split()[19]
assert payload['controller_pid'] == pid and payload['controller_start_ticks'] == start
assert payload['lifecycle_generation'] == 2 and payload['phase'] == 'preflight'
Path(sys.argv[3]).write_text(json.dumps({'pid': pid, 'registered': True}))
INNER
'''
        self.target(self.script)
        layout = layout_for_root(self.root)
        self.layout = replace(layout, descriptor=SimpleNamespace(descriptor=SimpleNamespace(
            service_uid=os.geteuid(), installation_id=self.payload['managed_execution']['installation_id'])))
        self.enterContext(patch.object(update_controller, 'layout_for_root', return_value=self.layout))
        self.enterContext(patch.object(managed_update, 'layout_for_root', return_value=self.layout))

    def target(self, script):
        (self.root/'update.sh').write_text(script);(self.root/'update.sh').chmod(0o700)
        self.fixture._git('add', 'update.sh');self.fixture._git('commit', '-m', 'Synthetic managed target')
        self.target_commit = self.fixture._git('rev-parse', 'HEAD')
        self.fixture._git('checkout', '--detach', self.fixture.previous)
        self.payload = journal(previous=self.fixture.previous, target=self.target_commit)
        self.payload.update(transaction_id=self.operation, managed_candidate=None, phase='queued')
        self.payload['managed_execution']['operation_id'] = self.operation
        self.fixture.journal.write_text(json.dumps(self.payload));self.fixture.journal.chmod(0o600)

    def prepared(self, root, lease):
        lease.require_current()
        self.assertEqual(root, self.root);self.assertFalse(self.marker.exists())
        self.assertEqual(state.inspect_state(root).generation, 2)
        update_controller.mutate_update_journal(root, operation_id=self.operation, generation=2,
            expected_lifecycle_phase='reserved', expected_detail_phases=frozenset({'queued'}),
            changes={'phase': 'preflight'})

    def run_engine(self):
        return update_controller._run_controller(self.root, operation_id=self.operation, generation=1,
            target_commit=self.target_commit, previous_commit=self.fixture.previous, automatic=False,
            install_mode='when_idle', update_branch='main', acknowledgement_fd=None, recovery=False,
            managed_initial=True)

    def test_child_cannot_use_workers_until_actual_pid_and_start_are_durably_registered(self):
        with patch.object(managed_update, 'prepare_initial', side_effect=self.prepared):
            self.assertEqual(self.run_engine(), 0)
        result = json.loads(self.marker.read_text())
        self.assertTrue(result['registered'])
        saved = update_controller.read_update_journal(self.fixture.journal)
        self.assertEqual(result['pid'], saved['controller_pid'])
        self.assertEqual(saved['managed_execution'], self.payload['managed_execution'])
        self.assertEqual(self.fixture._git('rev-parse', 'HEAD'), self.fixture.previous)

    def test_failed_registered_initial_shell_fences_canonical_and_subordinate_phase_before_releasing_lease(self):
        script = '''#!/usr/bin/env bash
set -uo pipefail
# StreamHome managed update engine: 1
IFS= read -r -n 1 -u "$STREAMHOME_MANAGED_UPDATE_GATE_FD" gate || exit 1
[[ "$gate" == 1 ]] || exit 1
exit 1
'''
        self.target(script)
        with patch.object(managed_update, 'prepare_initial', side_effect=self.prepared):
            self.assertEqual(self.run_engine(), 1)
        authority = state.inspect_state(self.root)
        self.assertEqual(authority.generation, 2)
        self.assertEqual(authority.active_operation.phase.value, 'recovery_required')
        payload = update_controller.read_update_journal(self.fixture.journal)
        self.assertEqual(payload['lifecycle_phase'], 'recovery_required')
        self.assertEqual(payload['managed_execution'], self.payload['managed_execution'])
        self.assertIsNone(state.read_controller_lease(self.root))
        self.assertIsNone(state.read_controller_lease(self.root))

    def test_preparation_failure_preserves_original_history_and_never_launches_target(self):
        def failed(root, lease):
            self.prepared(root, lease)
            raise RuntimeError('Injected preparation failure')
        with patch.object(managed_update, 'prepare_initial', side_effect=failed):
            with self.assertRaises(RuntimeError):self.run_engine()
        self.assertFalse(self.marker.exists())
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(update_controller.read_update_journal(self.fixture.journal)['managed_execution'], self.payload['managed_execution'])
        self.assertEqual(list((self.root/'.run').glob('update-controller.*.sh')), [])

    def test_missing_target_protocol_marker_refuses_before_launch(self):
        self.target('#!/usr/bin/env bash\nexit 82\n')
        with patch.object(managed_update, 'prepare_initial', side_effect=self.prepared):
            with self.assertRaises(update_controller.UpdateControllerConflict):self.run_engine()
        self.assertFalse(self.marker.exists())
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)

    def test_registration_failure_closes_gate_without_running_child_body_or_accepting(self):
        mutate = update_controller.mutate_update_journal
        def write(*args, **kwargs):
            if 'controller_pid' in kwargs['changes']:
                raise OSError('Injected child registration failure')
            return mutate(*args, **kwargs)
        with patch.object(managed_update, 'prepare_initial', side_effect=self.prepared), patch.object(
                update_controller, 'mutate_update_journal', side_effect=write):
            with self.assertRaises(OSError):self.run_engine()
        self.assertFalse(self.marker.exists())
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertFalse(update_controller.read_update_journal(self.fixture.journal)['runtime_committed'])

    def accepted_recovery_fixture(self, *, previous=False):
        parent = update_execution.parse(self.payload).parent
        template = update_execution.parse(journal()).candidate
        built = PreparedRuntime(template.runtime_bundle_id, template.python_environment_id,
            template.node_environment_id, template.web_bundle_id,
            template.runtime_manifest_sha256, template.web_asset_manifest_sha256)
        candidate = managed_update.prepared_execution(parent.execution, self.target_commit, built)
        selected = parent.execution if previous else candidate
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=1)
        lease, _ = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation, generation=1,
            expected_phase='reserved', owner_capability=capability, next_owner_kind='updater')
        lease.release()
        for old, new in (('reserved', 'exclusive'), ('exclusive', 'verifying'), ('verifying', 'recovery_required')):
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
                expected_phase=old, next_phase=new)
        payload = {**self.payload, 'lifecycle_generation': 2, 'lifecycle_phase': 'recovery_required',
            'phase': 'rolled_back' if previous else 'succeeded', 'managed_candidate': execution_tuple_payload(candidate),
            'runtime_committed': True, 'runtime_committed_at': 1, 'current_commit': selected.application_commit,
            'failed_target': self.target_commit if previous else '',
            'managed_verification_execution_sha256': execution_tuple_sha256(selected),
            'managed_accepted_execution_sha256': execution_tuple_sha256(selected)}
        self.fixture.journal.write_text(json.dumps(payload))
        return payload

    def test_fresh_root_recovery_registers_exact_materialized_child_before_running_body(self):
        script = self.gate + '''
/usr/bin/python3 - "$2/.run/update-state.json" "$BASHPID" "$2/.run/registered-child.json" <<'INNER'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());pid=int(sys.argv[2])
assert p['controller_pid']==pid and p['lifecycle_generation']==3
assert p['phase']=='recovering' and p['runtime_committed'] is True
assert p['runtime_committed_at']==1
Path(sys.argv[3]).write_text(json.dumps({'registered':True,'generation':3}))
INNER
'''
        self.target(script)
        payload = self.accepted_recovery_fixture()
        # Exact root grant/OS permission is a separately qualified boundary.
        with patch.object(update_recovery, 'require_takeover_permission') as permission, patch.object(
                update_recovery, 'require_controller_permission'), patch.object(update_recovery, 'prepare_transfer'):
            result = update_controller._run_controller(self.root, operation_id=self.operation, generation=2,
                target_commit=self.target_commit, previous_commit=self.fixture.previous, automatic=False,
                install_mode='when_idle', update_branch='main', acknowledgement_fd=None, recovery=True,
                operator_action='continue_recovery', pre_transfer_check=lambda: None,
                on_transfer=lambda generation: self.assertEqual(generation, 3), managed_recovery=True)
        self.assertEqual(result, 0)
        permission.assert_called_once_with(self.root, self.operation, 2, 'continue_recovery')
        self.assertEqual(json.loads(self.marker.read_text()), {'registered': True, 'generation': 3})
        saved = update_controller.read_update_journal(self.fixture.journal)
        self.assertEqual(saved['managed_execution'], payload['managed_execution'])
        self.assertEqual(saved['managed_accepted_execution_sha256'], payload['managed_accepted_execution_sha256'])


    def test_accepted_previous_uses_its_committed_engine_when_abandoned_target_git_object_is_unavailable(self):
        previous_body = self.gate + '''
/usr/bin/python3 - "$2/.run/update-state.json" "$BASHPID" "$2/.run/registered-child.json" <<'INNER'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
assert p['controller_pid']==int(sys.argv[2]) and p['lifecycle_generation']==3
assert p['current_commit']==p['previous_commit'] and p['runtime_committed'] is True
Path(sys.argv[3]).write_text(json.dumps({'selected':'previous'}))
INNER
'''
        self.target(previous_body)
        self.fixture.previous = self.target_commit
        self.fixture._git('checkout', '--detach', self.fixture.previous)
        self.target(self.gate+'exit 89\n')
        self.accepted_recovery_fixture(previous=True)
        abandoned = self.root/'.git/objects'/self.target_commit[:2]/self.target_commit[2:]
        retained = self.root.parent/'retained-abandoned-git-object'
        abandoned.rename(retained)
        with patch.object(update_recovery, 'require_takeover_permission'), patch.object(
                update_recovery, 'require_controller_permission'), patch.object(update_recovery, 'prepare_transfer'):
            result = update_controller._run_controller(self.root, operation_id=self.operation, generation=2,
                target_commit=self.target_commit, previous_commit=self.fixture.previous, automatic=False,
                install_mode='when_idle', update_branch='main', acknowledgement_fd=None, recovery=True,
                operator_action='continue_recovery', pre_transfer_check=lambda: None,
                on_transfer=lambda generation: self.assertEqual(generation, 3), managed_recovery=True)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.marker.read_text()), {'selected': 'previous'})
        self.assertTrue(retained.exists())
        self.assertEqual(self.fixture._git('rev-parse', 'HEAD'), self.fixture.previous)

    def retirement_proof(self, ready=True):
        from lifecycle.execution_state import ExecutionRecord
        from lifecycle import update_execution, update_history
        payload = update_controller.read_update_journal(self.fixture.journal)
        parent = update_execution.parse(payload).parent
        active = ExecutionRecord(parent.installation_id, parent.execution, self.operation, 0)
        self.enterContext(patch.object(managed_update, 'require_initial_controller',
            side_effect=lambda root, lease: managed_update.bound_journal(root, self.operation)))
        self.enterContext(patch.object(managed_update, 'verify_runtime'))
        self.enterContext(patch('lifecycle.runtime_links.verify_links'))
        self.enterContext(patch('lifecycle.control.rpc.request', return_value={'ready': ready}))
        self.enterContext(patch.object(update_history, 'read_active_execution', return_value=active))

    def test_proven_preparation_failure_retains_failure_and_completes_original_claim_without_runtime_changes(self):
        self.retirement_proof()
        def failed(root, lease):
            self.prepared(root, lease)
            raise RuntimeError('Injected target preparation failure')
        with patch.object(managed_update, 'prepare_initial', side_effect=failed):
            self.assertEqual(self.run_engine(), 1)
        self.assertFalse(self.marker.exists())
        self.assertIsNone(state.inspect_state(self.root).active_operation)
        self.assertEqual(state.inspect_state(self.root).generation, 2)
        self.assertIsNone(state.read_controller_lease(self.root))
        self.assertEqual(self.fixture._git('rev-parse', 'HEAD'), self.fixture.previous)
        from lifecycle import update_history
        retained = json.loads(update_history._path(self.root, self.operation, 2).read_text())
        self.assertEqual(retained['journal']['phase'], 'failed')
        self.assertEqual(retained['journal']['error'], 'preflight_failed')
        self.assertFalse(retained['journal']['runtime_committed'])
        self.assertEqual(retained['journal']['managed_execution'], self.payload['managed_execution'])

    def test_unconfirmed_original_transport_never_auto_completes_a_failed_preparation(self):
        self.retirement_proof(ready=1)  # A number must not masquerade as True.
        def failed(root, lease):
            self.prepared(root, lease)
            raise RuntimeError('Injected target preparation failure')
        with patch.object(managed_update, 'prepare_initial', side_effect=failed):
            with self.assertRaises(RuntimeError):self.run_engine()
        self.assertEqual(state.inspect_state(self.root).active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertFalse(self.marker.exists())
        self.assertFalse((state.lifecycle_paths(self.root).lifecycle_dir/'update-history').exists())
