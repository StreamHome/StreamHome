"""Finite UPDATE grant metadata with actual canonical review and explicit OS boundary."""
from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from lifecycle import update_recovery, update_controller, update_execution
from lifecycle.management_journal import execution_tuple_sha256
from lifecycle.control import update_grants as grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_recovery_review as review_fixture


class UpdateGrantTests(unittest.TestCase):
    def setUp(self):
        fixture = review_fixture.UpdateRecoveryReviewTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.bound = fixture.fixture.bound();self.active = fixture.active()
        self.journal = update_controller.read_update_journal(fixture.fixture.path)
        self.snapshot = fixture.review()
        self.scope = {'unit': 'streamhome-install-'+self.bound.parent.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation': 'a'*32, 'pid': 4321, 'start_identity': '123'}
        self.scope_check = self.enterContext(patch.object(grants, 'require_scope'))
        self.stop = self.enterContext(patch.object(grants, 'read_stop_epoch', return_value=self.snapshot['stop_epoch']))
        # Current production support9 is deliberately incompatible. This suite
        # qualifies the internal permission contract, not capability publication.
        self.reader = self.enterContext(patch('lifecycle.control.reader_support.require_managed_update_reader'))
        self.payload = {'operation_id': fixture.operation, 'review_id': str(uuid.uuid4()),
            'expected_evidence_sha256': self.snapshot['evidence_sha256'], 'action': 'continue_recovery',
            'issuer_scope': self.scope, 'boot_id': self.snapshot['boot_id'], 'stop_epoch': self.snapshot['stop_epoch']}
        self.review = self.enterContext(patch.object(update_recovery, 'review', return_value=self.snapshot))
        self.snapshot['runtime_status'] = 'active'

    def prepare(self):return grants.prepare(self.root, self.payload, control_manifest_sha256=self.fixture.digest)

    def test_finite_permission_binds_exact_accepted_tuple_active_record_and_original_parent(self):
        before = self.fixture.fixture.path.read_bytes(), self.fixture.active()
        grant = self.prepare();grants.require_binding(grant, self.bound, self.active, self.journal)
        self.assertEqual(grant['permitted_execution_sha256'], [self.snapshot['accepted_execution_sha256']])
        self.assertEqual(grant['generation'], 2)
        self.assertLessEqual(grant['expires_at_monotonic']-time.monotonic(), 900)
        self.assertGreater(grant['expires_at_monotonic']-time.monotonic(), 895)
        self.assertEqual((self.fixture.fixture.path.read_bytes(), self.fixture.active()), before)

    def test_unknown_fields_boolean_generations_nonfinite_expiry_and_foreign_actions_fail_closed(self):
        original = self.prepare()
        for changes in ({'schema_version': True}, {'generation': True}, {'original_generation': 3},
                {'expires_at_monotonic': True}, {'expires_at_monotonic': math.nan},
                {'expires_at_monotonic': math.inf}, {'operation_type': 'restore'}, {'action': 'rollback'},
                {'unknown': 1}, {'permitted_execution_sha256': []},
                {'permitted_execution_sha256': [original['candidate_execution_sha256']]},
                {'runtime_status': 'offline'}):
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                grants.parse_grant({**original, **changes})

    def test_accepted_permission_cannot_widen_to_other_runtime_or_revoke_acceptance(self):
        grant = self.prepare()
        candidate = replace(self.bound, accepted=self.bound.candidate, verification=self.bound.candidate)
        for bound in (candidate, replace(self.bound, accepted=None)):
            with self.subTest(bound=bound.accepted), self.assertRaises(ControlError):
                grants.require_binding(grant, bound, self.active, self.journal)
        with self.assertRaises(ControlError):
            grants.require_binding(grant, self.bound, replace(self.active, generation=4), self.journal)
        grants.require_binding(grant, self.bound, replace(self.active, generation=3), self.journal)
        with self.assertRaises(ControlError):
            grants.require_binding(grant, self.bound, replace(self.active, generation=3, operation_id=str(uuid.uuid4())), self.journal)

    def test_dead_issuer_stop_expiry_and_changed_retained_permission_are_refused(self):
        grant = self.prepare()
        for patcher in (patch.object(grants, 'require_scope', side_effect=ControlError('dead issuer')),
                patch.object(grants, 'read_stop_epoch', return_value='f'*64),
                patch.object(grants.time, 'monotonic', return_value=grant['expires_at_monotonic'])):
            with patcher, self.assertRaises(ControlError):grants.require_live(grant)
        with patch.object(grants, 'read_protected_json', side_effect=[grant, grant]):
            self.assertEqual(grants.read_grant(self.fixture.operation, 3), grant)
        changed = deepcopy(grant);changed['active_record_sha256'] = 'f'*64
        with patch.object(grants, 'read_protected_json', side_effect=[grant, changed]):
            with self.assertRaises(ControlError):grants.read_grant(self.fixture.operation, 3)

    def test_offline_continuation_or_unsupported_reader_cannot_prepare_permission(self):
        self.snapshot['runtime_status'] = 'offline'
        with self.assertRaises(ControlError):self.prepare()
        self.snapshot['runtime_status'] = 'active'
        self.reader.side_effect = ControlError('control_bound_reader_incompatible')
        with self.assertRaises(ControlError):self.prepare()

    def test_original_request_and_checkpoint_binding_cannot_change_after_review(self):
        grant = self.prepare()
        for changes in ({'automatic': True}, {'update_branch': 'other'},
                {'managed_database_checkpoint': None}):
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                grants.require_binding(grant, self.bound, self.active, {**self.journal, **changes})

    def test_fresh_controller_uses_only_its_current_grant_and_original_initial_owner_needs_none(self):
        grant = self.prepare();journal = {**self.journal, 'lifecycle_generation': 3}
        with patch.object(grants, 'read_grant', return_value=grant) as reader:
            self.assertEqual(update_recovery.require_controller_permission(self.root, journal, self.bound, 3), grant)
            reader.assert_called_once_with(self.fixture.operation, 3)
            reader.reset_mock()
            self.assertIsNone(update_recovery.require_controller_permission(self.root, self.journal, self.bound, 2))
            reader.assert_not_called()
        with patch.object(grants, 'read_grant', side_effect=ControlError('missing current grant')):
            with self.assertRaises(ControlError):
                update_recovery.require_controller_permission(self.root, journal, self.bound, 3)

    def test_first_transfer_does_not_ignore_an_existing_invalid_root_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'protected-permission-placeholder'
            path.write_text('Synthetic protected-path boundary')
            with patch.object(grants, 'grant_path', return_value=path), patch.object(grants, 'read_grant',
                    side_effect=ControlError('expired first-transfer permission')) as reader:
                with self.assertRaises(ControlError):
                    update_recovery.require_controller_permission(self.root, self.journal, self.bound, 2)
                reader.assert_called_once_with(self.fixture.operation, 2)

    def test_stopped_grant_requires_separate_plan_binding_and_cannot_widen_accepted_runtime(self):
        self.snapshot['runtime_status'] = 'offline'
        self.payload['resumption_plan_id'] = str(uuid.uuid4())
        grant = self.prepare()
        self.assertEqual(grant['schema_version'], 2)
        self.assertEqual(grant['permitted_execution_sha256'], [self.snapshot['accepted_execution_sha256']])
        plan = {'schema_version': 2, 'owner_type': 'update', 'action': 'update_recovery',
            'operation_id': grant['operation_id'], 'generation': grant['generation']+1,
            'review_id': grant['review_id'], 'recovery_action': 'continue_recovery',
            'parent_binding_sha256': grant['parent_binding_sha256'],
            'stages': [{'execution_sha256': grant['accepted_execution_sha256']}]}
        record = {'plan': plan, 'issuer': {'scope': grant['issuer_scope']}}
        with patch('lifecycle.control.resumption_ticket.reviewed_plan', return_value=record):
            self.assertEqual(grants.require_resumption_plan(grant), record)
            grants.require_binding(grant, self.bound, self.active, self.journal)
            for changes in ({'owner_type': 'restore'}, {'review_id': str(uuid.uuid4())},
                    {'parent_binding_sha256': 'f'*64}, {'generation': grant['generation']}):
                with self.subTest(changes=changes), patch('lifecycle.control.resumption_ticket.reviewed_plan',
                        return_value={**record, 'plan': {**plan, **changes}}), self.assertRaises(ControlError):
                    grants.require_binding(grant, self.bound, self.active, self.journal)
        with self.assertRaises(ControlError):grants.parse_grant({**grant, 'action': 'safe_abort'})
        with self.assertRaises(ControlError):grants.parse_grant({**grant, 'runtime_status': 'active'})

    def test_abort_permission_cannot_authorize_verification_acceptance_checkpoint_or_cutover(self):
        journal = {**self.journal, 'phase': 'preflight', 'runtime_committed': False, 'runtime_committed_at': None,
            'managed_accepted_execution_sha256': None, 'managed_verification_execution_sha256': None,
            'managed_database_checkpoint': None}
        bound = update_execution.parse(journal)
        active = replace(self.active, generation=0, operation_id=str(uuid.uuid4()))
        grant = {**self.prepare(), 'action': 'safe_abort', 'accepted_execution_sha256': None,
            'runtime_status': 'offline',
            'database_checkpoint_sha256': update_recovery.digest(None),
            'active_record_sha256': update_recovery.digest(active.payload()),
            'permitted_execution_sha256': [execution_tuple_sha256(bound.parent.execution)]}
        grants.require_binding(grant, bound, active, journal)
        selected = execution_tuple_sha256(bound.parent.execution)
        for changes in ({'phase': 'starting', 'lifecycle_phase': 'verifying',
                'managed_verification_execution_sha256': selected},
                {'phase': 'recovering', 'lifecycle_phase': 'verifying', 'runtime_committed': True,
                    'runtime_committed_at': 1, 'managed_accepted_execution_sha256': selected,
                    'managed_verification_execution_sha256': selected},
                {'web_artifacts_swapped': True}, {'web_dependencies_swapped': True}):
            payload = {**journal, **changes};parsed = update_execution.parse(payload)
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                grants.require_binding(grant, parsed, active, payload)
