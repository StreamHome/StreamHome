from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from lifecycle import update_execution as binding, update_controller
from lifecycle.management_journal import execution_tuple_sha256
from tests.lifecycle.test_management_journal import _execution_payload


def journal(previous='a'*40, target='e'*40):
    old = _execution_payload();old['application_commit'] = previous
    candidate = _execution_payload()
    for field in binding.PRESERVED:
        candidate[field] = deepcopy(old[field])
    candidate.update(application_commit=target, application_build_id=target[:12])
    operation = str(uuid.uuid4())
    parent = {'schema_version': 1, 'installation_id': str(uuid.uuid4()), 'operation_id': operation,
        'operation_type': 'update', 'original_generation': 1, 'execution': old,
        'supervisor_invocation_id': '1'*32, 'supervisor_pid': 2345,
        'supervisor_start_identity': '123', 'boot_id': str(uuid.uuid4()), 'stop_epoch': 'b'*64}
    return {'managed_update_version': 1, 'managed_execution': parent, 'managed_candidate': candidate,
        'managed_accepted_execution_sha256': None, 'managed_verification_execution_sha256': None, 'managed_database_checkpoint': None, 'transaction_id': operation, 'lifecycle_generation': 1,
        'lifecycle_phase': 'reserved', 'phase': 'preflight', 'runtime_committed': False,
        'previous_commit': previous, 'target_commit': target, 'current_commit': previous,
        'automatic': False, 'install_mode': 'when_idle', 'update_branch': 'main'}


class BoundUpdateExecutionTests(unittest.TestCase):
    def test_checkpoint_can_bind_only_once_before_activation_under_original_controller_generation(self):
        payload = journal();payload.update(lifecycle_generation=2, lifecycle_phase='exclusive', phase='stopping')
        parent = binding.parse(payload).parent
        record = binding.DatabaseCheckpoint(parent.operation_id, 2, execution_tuple_sha256(parent.execution), 'f'*64, 4096)
        prepared = {**payload, 'managed_database_checkpoint': record.payload()}
        binding.require_transition(payload, prepared)
        self.assertEqual(binding.parse(prepared).checkpoint, record)
        for change in ({'phase': 'installing'}, {'lifecycle_generation': 3}, {'lifecycle_phase': 'verifying'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                binding.require_transition({**payload, **change}, {**prepared, **change})
        for checkpoint in (None, {**record.payload(), 'sha256': 'e'*64}):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(ValueError):
                binding.require_transition(prepared, {**prepared, 'managed_database_checkpoint': checkpoint})
        for field, invalid in (('generation', True), ('size_bytes', True), ('size_bytes', 0),
                ('operation_id', str(uuid.uuid4())), ('previous_execution_sha256', 'a'*64)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                binding.parse({**prepared, 'managed_database_checkpoint': {**record.payload(), field: invalid}})
        # A takeover retains the original snapshot generation, never reattributes it.
        advanced = {**prepared, 'lifecycle_generation': 3, 'lifecycle_phase': 'recovery_required'}
        binding.require_transition(prepared, advanced)
        self.assertEqual(binding.parse(advanced).checkpoint, record)

    def test_legacy_is_distinct_from_partial_or_malformed_managed_evidence(self):
        self.assertIsNone(binding.parse({'phase': 'queued'}))
        payload = journal()
        for field in binding.FIELDS:
            changed = deepcopy(payload);changed.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):binding.parse(changed)
        for change in ({'managed_update_version': True}, {'lifecycle_generation': True},
                {'previous_commit': 'f'*40}, {'target_commit': 'not-a-commit'}):
            with self.subTest(change=change), self.assertRaises(ValueError):binding.parse({**payload, **change})

    def test_preparation_pins_deployment_control_and_provider_but_changes_runtime_generations(self):
        payload = journal();parsed = binding.parse(payload)
        self.assertIsNone(parsed.accepted)
        for field in binding.PRESERVED:
            changed = deepcopy(payload)
            changed['managed_candidate'][field] = _execution_payload()[field]
            if changed['managed_candidate'][field] == payload['managed_candidate'][field]:
                changed['managed_candidate'][field] = 2 if field == 'control_protocol_version' else {}
            with self.subTest(field=field), self.assertRaises(ValueError):binding.parse(changed)
        for field in ('runtime_bundle_id', 'python_environment_id', 'node_environment_id', 'web_bundle_id'):
            changed = deepcopy(payload);changed['managed_candidate'][field] = payload['managed_execution']['execution'][field]
            with self.subTest(field=field), self.assertRaises(ValueError):binding.parse(changed)

    def test_same_commit_generations_still_need_exact_acceptance_and_cannot_revoke_it(self):
        payload = journal(target='a'*40);parsed = binding.parse(payload)
        selected = execution_tuple_sha256(parsed.candidate)
        payload.update(lifecycle_phase='verifying', phase='starting', managed_verification_execution_sha256=selected)
        accepted = {**payload, 'runtime_committed': True, 'managed_accepted_execution_sha256': selected}
        self.assertEqual(binding.parse(accepted).accepted, parsed.candidate)
        binding.require_transition(payload, accepted)
        previous = {**accepted, 'managed_accepted_execution_sha256': execution_tuple_sha256(parsed.parent.execution),
            'managed_verification_execution_sha256': execution_tuple_sha256(parsed.parent.execution)}
        self.assertEqual(binding.parse(previous).accepted, parsed.parent.execution)
        with self.assertRaises(ValueError):binding.require_transition(accepted, previous)
        with self.assertRaises(ValueError):binding.require_transition(accepted, payload)
        for invalid in (None, 'f'*64, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                binding.parse({**accepted, 'managed_accepted_execution_sha256': invalid})

    def test_candidate_binding_is_once_before_cutover_and_original_request_is_immutable(self):
        payload = journal();unprepared = {**payload, 'managed_candidate': None}
        binding.require_transition(unprepared, payload)
        with self.assertRaises(ValueError):binding.require_transition(payload, unprepared)
        with self.assertRaises(ValueError):
            binding.require_transition({**unprepared, 'lifecycle_phase': 'exclusive'}, payload)
        for field, value in (('target_commit', 'f'*40), ('automatic', True), ('update_branch', 'other')):
            changed = {**unprepared, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                binding.require_transition(unprepared, changed)
        for phase in ('installing', 'starting', 'recovering'):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                binding.parse({**unprepared, 'phase': phase})

    def test_original_journal_reader_refuses_incomplete_binding_without_rewriting_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'update-state.json'
            payload = journal();path.write_text(json.dumps(payload));path.chmod(0o600)
            self.assertEqual(update_controller.read_update_journal(path), payload)
            payload['managed_candidate'] = None;payload['phase'] = 'starting'
            path.write_text(json.dumps(payload));original = path.read_bytes()
            with self.assertRaises(update_controller.UpdateControllerPersistenceError):
                update_controller.read_update_journal(path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':unittest.main()
