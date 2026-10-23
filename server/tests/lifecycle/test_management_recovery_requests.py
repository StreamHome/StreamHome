from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import recover as command, recovery_requests as grants
from lifecycle.control.requests import parse_management_request
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_management_requests import request_payload


class RecoveryGrantTests(unittest.TestCase):
    def setUp(self):
        self.original = parse_management_request(request_payload())
        self.current = replace(self.original, expected_generation=self.original.expected_generation + 1,
            supervisor=replace(self.original.supervisor, pid=127), backend=None,
            previous_boot_enabled=True, boot_enabled=True, stop_epoch='d' * 64)
        self.grant = {'schema_version': 1, 'recovery_review_id': str(uuid.uuid4()), 'source_evidence_sha256': 'e' * 64,
            'source_request_sha256': self.original.sha256, 'action': 'continue_recovery', 'request': self.current.payload(),
            'expires_at_monotonic': 1000.0}

    def resuming_grant(self):
        current = replace(self.current, supervisor=self.original.supervisor)
        scope = {'unit': 'streamhome-install-' + current.installation_id + '-' + '2' * 32 + '.scope',
                 'invocation': '3' * 32, 'pid': 100, 'start_identity': '200'}
        return {**self.grant, 'schema_version': 3, 'request': current.payload(),
                'issuer_scope': scope, 'resumption_plan_id': str(uuid.uuid4())}

    def test_bound_repair_grant_retains_history_and_can_never_be_a_start_grant(self):
        value = self.resuming_grant();value.pop('resumption_plan_id')
        current = replace(self.original, expected_generation=self.current.expected_generation, backend=None,
                          stop_epoch=self.current.stop_epoch)
        value.update(schema_version=4, action='repair_bound_environment', request=current.payload(),
            repair_review_sha256='a' * 64, execution_sha256='b' * 64, filesystem_sha256='c' * 64)
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            self.assertEqual(grants.parse_grant(value), value)
            for changed in ({'resumption_plan_id': str(uuid.uuid4())}, {'action': 'continue_recovery'}, {'filesystem_sha256': 'bad'}):
                with self.subTest(changed=changed), self.assertRaises((ControlError, ValueError)):
                    grants.parse_grant({**value, **changed})

    def test_resumption_preserves_original_supervisor_history_without_claiming_it_is_live(self):
        value = self.resuming_grant()
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            self.assertEqual(grants.parse_grant(value), value)
            changed = deepcopy(value);changed['request']['supervisor'] = self.current.supervisor.payload()
            with self.assertRaises(ControlError):grants.parse_grant(changed)
            missing = dict(value);missing.pop('resumption_plan_id')
            with self.assertRaises(ControlError):grants.parse_grant(missing)

    def test_live_resumption_grant_requires_its_exact_root_plan_but_history_survives_issuer_death(self):
        from lifecycle.control import stopped_repair, resumption_ticket
        value = self.resuming_grant();current = parse_management_request(value['request'])
        record = {'plan': {'operation_id': current.operation_id, 'generation': current.expected_generation + 1,
            'review_id': value['recovery_review_id'], 'owner_type': 'management', 'action': current.action.value},
            'issuer': {'scope': value['issuer_scope']}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(grants.request_contract, 'read_management_request', return_value=self.original))
            stack.enter_context(patch.object(grants, 'read_protected_json', return_value=value))
            stack.enter_context(patch.object(grants.Path, 'read_text', return_value=current.boot_id))
            stack.enter_context(patch.object(grants, 'read_stop_epoch', return_value=current.stop_epoch))
            stack.enter_context(patch.object(grants.time, 'monotonic', return_value=100))
            live = stack.enter_context(patch.object(stopped_repair, 'require_scope'))
            selected = stack.enter_context(patch.object(resumption_ticket, 'reviewed_plan', return_value=record))
            self.assertEqual(grants.read_grant(current.operation_id, current.expected_generation + 1), value)
            selected.assert_called_once_with(value['resumption_plan_id'], installation_id=current.installation_id,
                control_manifest_sha256=current.controller_control_manifest_sha256)
            record['plan']['generation'] += 1
            with self.assertRaises(ControlError):grants.read_grant(current.operation_id, current.expected_generation + 1)
            live.side_effect = ControlError('control_stopped_repair_issuer_changed')
            selected.side_effect = ControlError('control_resumption_review_changed')
            self.assertEqual(grants.read_grant(current.operation_id, current.expected_generation + 1, require_live=False), value)

    def test_prepare_resumption_uses_actual_stopped_proof_and_retains_original_request_identities(self):
        value = self.resuming_grant()
        snapshot = {'operation_id': self.original.operation_id, 'generation': self.current.expected_generation,
            'evidence_sha256': 'e' * 64, 'initial_service_state': 'running', 'activation_mode': 'cutover',
            'supervisor_invocation_id': self.original.supervisor_invocation_id}
        payload = {'operation_id': self.original.operation_id, 'expected_evidence_sha256': 'e' * 64,
            'recovery_review_id': value['recovery_review_id'], 'boot_enabled': True, 'boot_id': self.current.boot_id,
            'stop_epoch': self.current.stop_epoch, 'issuer_scope': value['issuer_scope'],
            'resumption_plan_id': value['resumption_plan_id'],
            'processes': {'initial_service_state': 'stopped', 'supervisor_invocation_id': None, 'supervisor': None, 'backend': None}}
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original), patch.object(grants, 'review', return_value=snapshot), patch('lifecycle.control.stopped_repair.require_scope'), patch('lifecycle.control.reader_support.require_resumption_reader') as reader:
            result = grants.prepare(payload, control_manifest_sha256=self.original.controller_control_manifest_sha256)
        reader.assert_called_once_with(self.original.candidate)
        self.assertEqual(result['schema_version'], 3)
        self.assertEqual(result['request']['supervisor'], self.original.supervisor.payload())
        self.assertEqual(result['request']['supervisor_invocation_id'], self.original.supervisor_invocation_id)
        self.assertIsNone(result['request']['backend'])

    def test_recovery_preserves_reviewed_tuple_settings_and_original_intent(self):
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            self.assertEqual(grants.parse_grant(self.grant), self.grant)
            for key, value in (('candidate', self.original.payload()['previous']), ('deployment', {**self.current.deployment.payload(), 'web_port': 4444}),
                               ('supervisor_invocation_id', '1' * 32), ('initial_service_state', 'stopped')):
                changed = deepcopy(self.grant)
                changed['request'][key] = value
                with self.subTest(key=key), self.assertRaises((ValueError, ControlError)):
                    grants.parse_grant(changed)

    def test_expiry_stop_and_wrong_generation_block_even_valid_root_grants(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(grants.request_contract, 'read_management_request', return_value=self.original))
            stack.enter_context(patch.object(grants, 'read_protected_json', return_value=self.grant))
            stack.enter_context(patch.object(grants.Path, 'read_text', return_value=self.current.boot_id))
            stack.enter_context(patch.object(grants, 'read_stop_epoch', return_value=self.current.stop_epoch))
            clock = stack.enter_context(patch.object(grants.time, 'monotonic', return_value=100))
            self.assertEqual(grants.read_grant(self.current.operation_id, self.current.expected_generation + 1), self.grant)
            clock.return_value = 1001
            with self.assertRaisesRegex(ControlError, 'control_recovery_permission_expired'):
                grants.read_grant(self.current.operation_id, self.current.expected_generation + 1)
            self.assertEqual(grants.read_grant(self.current.operation_id, self.current.expected_generation + 1, require_live=False), self.grant)
            clock.return_value = 100
            with self.assertRaises(ControlError):
                grants.read_grant(self.current.operation_id, self.current.expected_generation + 2)
            self.grant['request']['stop_epoch'] = 'f' * 64
            with self.assertRaisesRegex(ControlError, 'control_recovery_permission_expired'):
                grants.read_grant(self.current.operation_id, self.current.expected_generation + 1)

    def test_unknown_nonfinite_and_boolean_expiry_are_not_permissions(self):
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            for extra in ({'expires_at_monotonic': True}, {'expires_at_monotonic': float('nan')}, {'command': 'arbitrary'}, {'action': 'rollback'}):
                with self.subTest(extra=extra), self.assertRaises(ControlError):
                    grants.parse_grant({**self.grant, **extra})

    def preserving_grant(self, mode='save_only', running=True):
        original = replace(self.original, activation_mode=grants.contract.ActivationMode(mode),
            interruption_approved=False, boot_enabled=False, previous_boot_enabled=True)
        if mode == 'os_only':
            original = replace(original, action=grants.contract.ManagementAction.PROVISION_CONTROL, candidate=original.previous)
        if not running:
            original = replace(original, initial_service_state='stopped', supervisor_invocation_id=None, supervisor=None, backend=None)
        current = replace(original, expected_generation=original.expected_generation + 1,
            previous_boot_enabled=True, stop_epoch='d' * 64)
        return original, {**self.grant, 'source_request_sha256': original.sha256, 'request': current.payload()}

    def test_preserving_grant_keeps_original_boot_target_and_exact_running_processes(self):
        for mode in ('save_only', 'os_only'):
            original, grant = self.preserving_grant(mode)
            with self.subTest(mode=mode), patch.object(grants.request_contract, 'read_management_request', return_value=original):
                self.assertEqual(grants.parse_grant(grant), grant)
                for key, changed in (('boot_enabled', True), ('backend', {'pid': 9001, 'start_identity': '9'}),
                                     ('supervisor', {'pid': 9002, 'start_identity': '8'}), ('backend', None)):
                    value = deepcopy(grant)
                    value['request'][key] = changed
                    with self.subTest(key=key), self.assertRaises(ControlError):
                        grants.parse_grant(value)

    def test_stopped_preserving_grant_never_turns_into_a_start_permission(self):
        original, grant = self.preserving_grant(running=False)
        with patch.object(grants.request_contract, 'read_management_request', return_value=original):
            self.assertEqual(grants.parse_grant(grant), grant)
            value = deepcopy(grant)
            value['request'].update(initial_service_state='running', supervisor_invocation_id='f' * 32,
                supervisor={'pid': 100, 'start_identity': '9'}, backend={'pid': 101, 'start_identity': '10'})
            with self.assertRaises(ControlError):
                grants.parse_grant(value)

    def test_stopped_recovery_scope_is_required_live_but_not_for_historical_evidence(self):
        from lifecycle.control import stopped_repair
        original, value = self.preserving_grant(running=False)
        scope = {'unit': 'streamhome-install-' + original.installation_id + '-' + '2' * 32 + '.scope',
                 'invocation': '3' * 32, 'pid': 100, 'start_identity': '200'}
        value.update(schema_version=2, issuer_scope=scope)
        current = parse_management_request(value['request'])
        with ExitStack() as stack:
            stack.enter_context(patch.object(grants.request_contract, 'read_management_request', return_value=original))
            stack.enter_context(patch.object(grants, 'read_protected_json', return_value=value))
            stack.enter_context(patch.object(grants.Path, 'read_text', return_value=current.boot_id))
            stack.enter_context(patch.object(grants, 'read_stop_epoch', return_value=current.stop_epoch))
            stack.enter_context(patch.object(grants.time, 'monotonic', return_value=100))
            live = stack.enter_context(patch.object(stopped_repair, 'require_scope'))
            self.assertEqual(grants.read_grant(current.operation_id, current.expected_generation + 1), value)
            live.assert_called_once_with(scope, original.installation_id)
            live.side_effect = ControlError('control_stopped_repair_issuer_changed')
            with self.assertRaises(ControlError):
                grants.read_grant(current.operation_id, current.expected_generation + 1)
            self.assertEqual(grants.read_grant(current.operation_id, current.expected_generation + 1, require_live=False), value)

    def test_running_grant_cannot_borrow_an_offline_scope(self):
        value = {**self.grant, 'schema_version': 2,
                 'issuer_scope': {'unit': 'streamhome-install-' + self.original.installation_id + '-' + '2' * 32 + '.scope',
                                  'invocation': '3' * 32, 'pid': 100, 'start_identity': '200'}}
        with patch.object(grants.request_contract, 'read_management_request', return_value=self.original):
            with self.assertRaises(ControlError):
                grants.parse_grant(value)

    def test_preserving_prepare_finishes_original_boot_preference_under_a_new_review(self):
        original, grant = self.preserving_grant()
        snapshot = {'operation_id': original.operation_id, 'generation': original.expected_generation + 1,
            'evidence_sha256': 'e' * 64, 'initial_service_state': 'running',
            'supervisor_invocation_id': original.supervisor_invocation_id}
        payload = {'operation_id': original.operation_id, 'expected_evidence_sha256': 'e' * 64,
            'recovery_review_id': str(uuid.uuid4()), 'boot_enabled': True, 'boot_id': original.boot_id,
            'stop_epoch': original.stop_epoch, 'processes': {'initial_service_state': 'running',
                'supervisor_invocation_id': original.supervisor_invocation_id,
                'supervisor': original.supervisor.payload(), 'backend': original.backend.payload()}}
        with patch.object(grants.request_contract, 'read_management_request', return_value=original), patch.object(grants, 'review', return_value=snapshot):
            result = grants.prepare(payload, control_manifest_sha256=original.controller_control_manifest_sha256)
        self.assertFalse(result['request']['boot_enabled'])
        self.assertTrue(result['request']['previous_boot_enabled'])
        self.assertEqual(result['source_request_sha256'], original.sha256)

    def test_noninteractive_decline_and_stale_review_never_imply_consent(self):
        snapshot = {'operation_id': self.current.operation_id, 'generation': 8, 'evidence_sha256': 'e' * 64}
        value = {'schema_version': 1, 'operation_id': self.current.operation_id, 'expected_generation': 8,
            'expected_evidence_sha256': 'e' * 64, 'continue_recovery': True, 'interruption_approved': True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.json'
            path.write_text(json.dumps(value))
            self.assertTrue(command._consent(path, snapshot))
            path.write_text(json.dumps({**value, 'interruption_approved': False}))
            self.assertFalse(command._consent(path, snapshot))
            path.write_text(json.dumps({**value, 'expected_generation': 7}))
            with self.assertRaisesRegex(ControlError, 'control_recovery_review_stale'):
                command._consent(path, snapshot)

    def test_forward_consent_cannot_be_reused_as_rollback_or_abort_consent(self):
        snapshot = {'operation_id': self.current.operation_id, 'generation': 8, 'evidence_sha256': 'e' * 64}
        value = {'schema_version': 1, 'operation_id': self.current.operation_id, 'expected_generation': 8,
            'expected_evidence_sha256': 'e' * 64, 'continue_recovery': True, 'interruption_approved': True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            path.write_text(json.dumps(value))
            for action in ('rollback', 'safe_abort'):
                with self.subTest(action=action), self.assertRaises(ControlError):
                    command._consent(path, snapshot, action=action)
                path.write_text(json.dumps({**value, 'schema_version': 2, 'action': action}))
                self.assertTrue(command._consent(path, snapshot, action=action))
                with self.assertRaises(ControlError):
                    command._consent(path, snapshot)
                path.write_text(json.dumps(value))

    def test_root_recovery_command_does_not_import_canonical_or_application_code(self):
        script = "import sys; import lifecycle.control.recover; assert not {'lifecycle.state','lifecycle.control.recovery_requests','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
