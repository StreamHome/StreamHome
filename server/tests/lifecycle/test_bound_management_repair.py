from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_management_repair as bound, management_journal as contract, management_recovery_store as recovery, management_store, runtime_build as runtime, state
from lifecycle.control.requests import parse_management_request
from tests.lifecycle.test_management_journal import _journal_payload
from tests.lifecycle.test_management_requests import request_payload
from tests.lifecycle import test_runtime_recovery_cache as cache_fixture


class BoundManagementRepairTests(unittest.TestCase):
    def setUp(self):
        fixture = cache_fixture.BoundRecoveryCacheTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture = fixture;self.root = fixture.root;fixture.cache = self.root / 'runtime-recovery'
        asset = fixture.trees['web'] / 'assets.json';asset.write_bytes(b'{}\n');asset.chmod(0o600)
        fixture.manifest['web'] = runtime.tree_inventory(fixture.trees['web'])
        fixture.manifest['source'] = {'server/requirements.lock': hashlib.sha256(b'locked input').hexdigest()}
        raw = runtime.canonical_bytes(fixture.manifest);(fixture.bundle / 'manifest.json').write_bytes(raw)
        self.execution = replace(fixture.execution, runtime_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                                 web_asset_manifest_sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
        fixture.execution = self.execution;fixture.capture()
        for name in ('python', 'node', 'web', 'bundles'):
            (fixture.runtime / name).chmod(0o700)
        (self.root / 'server').mkdir();(self.root / 'start.sh').write_text('#!/bin/sh\n');(self.root / 'update.sh').write_text('#!/bin/sh\n')
        paths = state.lifecycle_paths(self.root);state._ensure_lifecycle_directories(paths)
        state._write_state_unlocked(paths, state.LifecycleState(2, 0, None))
        payload = _journal_payload();payload.update(previous=contract.execution_tuple_payload(self.execution),
            candidate=contract.execution_tuple_payload(replace(self.execution, deployment_revision=str(uuid.uuid4()))))
        self.lease, self.claim = state.claim_management_controller(self.root, operation_id=payload['operation_id'], expected_generation=0)
        payload['generation'] = self.claim.state.generation;self.source = contract.parse_management_journal(payload)
        management_store.create_management_journal(self.root, self.source, lease=self.lease)
        self.addCleanup(lambda: self.lease.release())
        self.layout = SimpleNamespace(managed=True, cache=self.root,
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(service_uid=__import__('os').geteuid())))
        self.enterContext(patch.object(bound, 'ROOT', self.root))
        self.enterContext(patch.object(bound, 'layout_for_root', return_value=self.layout))
        self.enterContext(patch.object(bound, 'require_stopped'))
        self.enterContext(patch.object(bound, 'require_service_quiescent'))
        self.enterContext(patch('lifecycle.control.reader_support.require_bound_repair_reader'))
        self.enterContext(patch.object(runtime, 'source_inputs', return_value={'server/requirements.lock': b'locked input'}))
        self.enterContext(patch.object(bound.recovery_requests, 'review', side_effect=self.base_review))
        (fixture.trees['python'] / 'bin/program').write_bytes(b'broken selected environment')
        self.database = self.root / 'database-sentinel';self.database.write_bytes(b'preserve parent data')

    def base_review(self, operation_id, **kwargs):
        authority = state.inspect_state(self.root);journal = management_store.inspect_management_journal(self.root, operation_id)
        return {'operation_id': operation_id, 'installation_id': journal.installation_id, 'generation': authority.generation,
            'phase': authority.active_operation.phase.value, 'accepted': journal.acceptance is not None,
            'evidence_sha256': 'a' * 64, 'permitted_actions': ['continue_recovery'],
            'selected_execution_sha256': contract.execution_tuple_sha256(contract.selected_execution(journal))}

    def controller(self):
        snapshot = bound.plan(self.source.operation_id, control_manifest_sha256=self.execution.control_manifest_sha256)
        reviewed = request_payload();reviewed.update(installation_id=self.source.installation_id, operation_id=self.source.operation_id,
            expected_generation=1, review_id=self.source.review_id, previous=contract.execution_tuple_payload(self.source.previous),
            candidate=contract.execution_tuple_payload(self.source.candidate), supervisor_invocation_id=self.source.supervisor_invocation_id,
            controller_control_manifest_sha256=self.execution.control_manifest_sha256)
        value = bound.BoundEnvironmentController.__new__(bound.BoundEnvironmentController)
        value.review = parse_management_request(reviewed);value.layout = self.layout;value.invocation = '2' * 32
        value.lease = value.claim = value.journal = None
        value.grant = {'schema_version': 4, 'repair_review_sha256': snapshot['evidence_sha256'],
            'execution_sha256': snapshot['selected_execution_sha256'], 'filesystem_sha256': snapshot['artifacts']['filesystem_sha256'],
            'source_evidence_sha256': snapshot['source_evidence_sha256'], 'recovery_review_id': str(uuid.uuid4())}
        value.checkpoint = lambda: value.lease.require_current() if value.lease else None
        self.lease.release();self.addCleanup(lambda: value.lease.release() if value.lease else None)
        return value

    def test_real_reconstruction_rotates_generation_and_keeps_the_exact_parent_unresolved(self):
        value = self.controller();value.execute()
        authority = state.inspect_state(self.root)
        self.assertEqual(authority.generation, 2)
        self.assertEqual(authority.active_operation.operation_id, self.source.operation_id)
        self.assertEqual(authority.active_operation.phase, state.LifecyclePhase.RECOVERY_REQUIRED)
        self.assertEqual(value.journal.previous, self.source.previous);self.assertEqual(value.journal.candidate, self.source.candidate)
        self.assertIsNone(value.journal.acceptance)
        runtime.verify_runtime(self.root, contract.selected_execution(value.journal))
        self.assertEqual(self.database.read_bytes(), b'preserve parent data')
        evidence = list(self.fixture.trees['python'].parent.glob('.repair-*'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0] / 'bin/program').read_bytes(), b'broken selected environment')
        with patch.object(bound.recovery_requests, 'read_grant', return_value=value.grant):
            self.assertEqual(bound.result(self.source.operation_id)['stage'], 'environment_repaired_recovery_required')

    def test_stale_filesystem_review_fails_before_generation_rotation(self):
        value = self.controller()
        (self.fixture.trees['node'] / 'bin/program').write_bytes(b'another change')
        with self.assertRaises(Exception):value.execute()
        self.assertEqual(state.inspect_state(self.root).generation, 1)
        self.assertIsNone(value.lease)

    def test_accepted_parent_acceptance_is_preserved_by_repair(self):
        exclusive = replace(self.source, stage=contract.ManagementStage.EXCLUSIVE)
        # Publish the required stage chain under the original owner.
        prepared = replace(self.source, stage=contract.ManagementStage.PREPARED)
        management_store.advance_management_journal(self.root, self.source, prepared, lease=self.lease)
        state.transition_operation_with_credential(self.root, operation_id=self.source.operation_id, generation=1,
            expected_phase='reserved', next_phase='exclusive')
        management_store.advance_management_journal(self.root, prepared, exclusive, lease=self.lease)
        state.transition_operation_with_credential(self.root, operation_id=self.source.operation_id, generation=1,
            expected_phase='exclusive', next_phase='verifying')
        verifying = replace(exclusive, stage=contract.ManagementStage.VERIFYING)
        management_store.advance_management_journal(self.root, exclusive, verifying, lease=self.lease)
        accepted = replace(verifying, stage=contract.ManagementStage.ACCEPTED,
            acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(verifying.candidate), 'a' * 64))
        management_store.advance_management_journal(self.root, verifying, accepted, lease=self.lease);self.source = accepted
        value = self.controller();value.execute()
        self.assertEqual(value.journal.acceptance, accepted.acceptance)
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)


if __name__ == '__main__':unittest.main()
