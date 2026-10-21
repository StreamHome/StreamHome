from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from lifecycle import management_journal as contract, state
from lifecycle.control.requests import parse_management_request
from lifecycle.deployment_config import default_deployment
from tests.lifecycle.test_management_journal import _journal_payload


def request_payload():
    journal = _journal_payload()
    return {
        'schema_version': 1, 'installation_id': journal['installation_id'], 'operation_id': journal['operation_id'],
        'review_id': journal['review_id'], 'expected_generation': 7, 'action': journal['action'],
        'activation_mode': journal['activation_mode'], 'previous': journal['previous'], 'candidate': journal['candidate'],
        'expected_desired': {'revision': journal['previous']['deployment_revision'], 'sha256': 'a'*64},
        'deployment': default_deployment().payload(), 'initial_service_state': 'running',
        'supervisor_invocation_id': journal['supervisor_invocation_id'], 'supervisor': {'pid': 125, 'start_identity': '103'},
        'backend': {'pid': 126, 'start_identity': '104'}, 'interruption_approved': True,
        'previous_boot_enabled': False, 'boot_enabled': True, 'boot_id': str(uuid.uuid4()),
        'stop_epoch': 'b'*64, 'controller_control_manifest_sha256': 'c'*64,
    }


class ManagementRequestTests(unittest.TestCase):
    def test_roundtrip_is_canonical_and_review_digest_changes_with_boot_or_candidate(self):
        payload = request_payload()
        reviewed = parse_management_request(payload)
        self.assertEqual(reviewed.payload(), payload)
        self.assertEqual(parse_management_request(json.loads(json.dumps(payload))).sha256, reviewed.sha256)
        self.assertNotEqual(replace(reviewed, boot_enabled=False).sha256, reviewed.sha256)
        self.assertNotEqual(replace(reviewed, stop_epoch='d'*64).sha256, reviewed.sha256)

    def test_review_cannot_expand_configure_into_source_runtime_or_control_changes(self):
        for name, value in [('application_commit', 'f'*40), ('runtime_bundle_id', str(uuid.uuid4())), ('control_manifest_sha256', 'e'*64)]:
            payload = request_payload()
            payload['candidate'][name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_management_request(payload)

    def test_interruption_consent_and_process_shape_are_explicit(self):
        for patch in ({'interruption_approved': False}, {'interruption_approved': 1}, {'backend': {'pid': True, 'start_identity': '104'}}, {'supervisor': None}, {'supervisor_invocation_id': '0'*32}, {'shell_command': 'arbitrary'}):
            with self.subTest(patch=patch), self.assertRaises((ValueError, RuntimeError)):
                parse_management_request({**request_payload(), **patch})

    def test_independently_approved_cutover_does_not_require_unhealthy_backend_admission(self):
        payload = request_payload()
        payload['backend'] = None
        self.assertIsNone(parse_management_request(payload).backend)

    def test_os_only_cannot_change_the_application_tuple(self):
        payload = request_payload()
        payload.update(action='provision_control', activation_mode='os_only', interruption_approved=False)
        with self.assertRaises(ValueError):
            parse_management_request(payload)
        payload['candidate'] = deepcopy(payload['previous'])
        parse_management_request(payload)

    def test_privileged_command_imports_do_not_load_canonical_or_application_modules(self):
        code = "import sys; import lifecycle.control.administration; assert not {'lifecycle.state','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys_executable(), '-c', code], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_root_supervisor_dispatch_rejects_before_authority_module_import(self):
        code = "from pathlib import Path; import sys; from unittest.mock import patch; from lifecycle.control import privileged\nwith patch.object(privileged.os,'geteuid',return_value=0):\n assert privileged.dispatch(['--supervisor'],bundle=Path('/unused'),manifest={},manifest_sha256='a'*64,rescue=False)==2\nassert not {'lifecycle.state','lifecycle.control.supervisor','config','application'}.intersection(sys.modules)"
        result = subprocess.run([sys_executable(), '-c', code], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


def sys_executable():
    import sys
    return sys.executable


class AtomicManagementClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'server').mkdir()
        for name in ('start.sh', 'update.sh'):
            (self.root/name).write_text('#!/bin/sh\n')
        self.paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(self.paths)
        state._write_state_unlocked(self.paths, state.LifecycleState(2, 7, None))

    def test_stale_review_never_claims_or_leaves_an_owner_credential(self):
        operation = str(uuid.uuid4())
        before = self.paths.state_path.read_bytes()
        with self.assertRaises(state.LifecycleConflictError):
            state.claim_management_controller(self.root, operation_id=operation, expected_generation=6)
        self.assertEqual(self.paths.state_path.read_bytes(), before)
        self.assertFalse(self.paths.credential_path(operation).exists())
        self.assertIsNone(state.read_controller_lease(self.root))

    def test_finishing_controller_exclusion_precedes_the_next_canonical_claim(self):
        lease, claim = state.claim_management_controller(self.root, operation_id=str(uuid.uuid4()), expected_generation=7)
        try:
            state.complete_operation_with_credential(self.root, operation_id=lease.lease.operation_id, generation=8, expected_phase='reserved')
            before = self.paths.state_path.read_bytes()
            with self.assertRaises(state.ControllerLockError):
                state.claim_management_controller(self.root, operation_id=str(uuid.uuid4()), expected_generation=8)
            self.assertEqual(self.paths.state_path.read_bytes(), before)
        finally:
            lease.release()
        next_lease, next_claim = state.claim_management_controller(self.root, operation_id=str(uuid.uuid4()), expected_generation=8)
        try:
            self.assertEqual(next_claim.state.generation, 9)
            next_lease.require_current()
        finally:
            next_lease.release()


if __name__ == '__main__':
    unittest.main()
