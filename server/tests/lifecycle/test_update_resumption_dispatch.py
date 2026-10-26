"""Real one-use protected store with substituted root ownership and systemd calls."""
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from lifecycle.control import update_grants, resumption_dispatch as dispatch, resumption_store
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_parent_resumption_dispatch as parent_fixture


class UpdateResumptionDispatchTests(unittest.TestCase):
    def setUp(self):
        parent = parent_fixture.ParentResumptionDispatchTests();parent.setUp()
        self.addCleanup(parent.doCleanups);self.parent = parent;self.fixture = parent.fixture
        self.plan = parent.plan;self.plan.update(owner_type='update', action='update_recovery')
        (self.fixture.fixture.root/self.plan['plan_id']/'plan.json').write_text(json.dumps(
            {'plan': self.plan, 'issuer': self.fixture.fixture.evidence}))
        selected = self.plan['stages'][0]['execution_sha256']
        self.grant = {**parent.grant, 'operation_type': 'update', 'original_generation': 1,
            'source_lifecycle_phase': 'recovery_required', 'active_record_sha256': '4'*64,
            'request_sha256': '5'*64, 'database_checkpoint_sha256': '6'*64,
            'runtime_link_source_sha256': '7'*64,
            'previous_execution_sha256': selected, 'candidate_execution_sha256': 'f'*64,
            'accepted_execution_sha256': selected, 'permitted_execution_sha256': [selected],
            'runtime_status': 'offline'}
        self.grant.pop('execution_sha256')
        self.fixture.grant = self.grant
        self.fixture.worker.return_value.update(owner_type='update', action='update_recovery')
        self.enterContext(patch.object(update_grants, 'parse_scope'))
        self.enterContext(patch.object(update_grants, 'require_live'))
        read = dispatch.read_protected_json
        self.enterContext(patch.object(dispatch, 'read_protected_json', side_effect=lambda path:
            self.grant if path.name.startswith('update-recovery-') else read(path)))

    def start(self):return dispatch.start(self.grant, control_manifest_sha256=self.fixture.manifest)

    def test_exact_update_start_consumes_once_and_only_observes_its_original_bound_invocation(self):
        self.assertTrue(self.start());self.assertTrue(self.start())
        self.assertEqual(self.fixture.systemctl.call_count, 1)
        self.assertEqual(self.fixture.worker.call_count, 1)
        receipt = json.loads(self.fixture.fixture.receipt_path().read_text())
        self.assertEqual(receipt['status'], 'bound')

    def test_pending_or_foreign_update_plan_never_consumes_or_dispatches(self):
        self.fixture.worker.return_value = {'pending': True}
        with patch.object(dispatch,'qualify',wraps=dispatch.qualify) as qualify:
            self.assertFalse(self.start())
        self.assertEqual(qualify.call_count,2)
        self.assertFalse(self.fixture.fixture.receipt_path().exists())
        self.fixture.systemctl.assert_not_called()
        for changes in ({'review_id': self.plan['installation_id']}, {'generation': True},
                {'action': 'safe_abort'}, {'parent_binding_sha256': 'f'*64},
                {'permitted_execution_sha256': ['f'*64]}, {'schema_version': 1}):
            original = dict(self.grant);self.grant.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ControlError):self.start()
            self.grant.clear();self.grant.update(original)
        self.fixture.systemctl.assert_not_called()

    def test_prestart_reads_update_permission_and_repeats_independent_observation_before_binding_once(self):
        resumption_store.consume_stage(self.plan['plan_id'], 'candidate_start',
            self.fixture.worker.return_value, control_manifest_sha256=self.fixture.manifest)
        dispatch.before_start(control_manifest_sha256=self.fixture.manifest)
        self.assertEqual(self.fixture.worker.call_args.kwargs['payload']['startup_hook']['pid'], os.getpid())
        with self.assertRaises(ControlError):dispatch.before_start(control_manifest_sha256=self.fixture.manifest)

    def test_lost_os_reply_cannot_dispatch_update_start_again(self):
        self.fixture.systemctl.side_effect = subprocess.TimeoutExpired('fixed start', 30)
        with self.assertRaises(ControlError):self.start()
        with self.assertRaises(ControlError):self.start()
        self.assertEqual(self.fixture.systemctl.call_count, 1)
