"""Controller-side stopped proof and bounded wait never issue OS start."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lifecycle import managed_update, state
from lifecycle.control import update_resumption_helpers as helpers
from lifecycle.control import update_resumption_runtime, update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_resumption_observation as observation_fixture


class UpdateResumptionHelperTests(unittest.TestCase):
    def setUp(self):
        fixture = observation_fixture.UpdateResumptionObservationTests();fixture.setUp();fixture.ready()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.operation
        self.proof = SimpleNamespace(lease=fixture.lease.lease, require_current=Mock())
        self.actor = {'ActiveState': 'inactive', 'MainPID': '0', 'InvocationID': ''}
        self.unit = self.enterContext(patch.object(helpers, 'unit_snapshot', side_effect=lambda: self.actor))
        self.stopped = self.enterContext(patch.object(helpers, 'require_stopped'))
        self.quiet = self.enterContext(patch.object(helpers, 'require_service_quiescent'))
        self.enterContext(patch.object(helpers, 'require_owner_peer', side_effect=lambda root, lease, pid:
            managed_update.bound_journal(root, lease.operation_id)))

    def test_offline_requires_exact_fresh_permission_stopped_processes_and_repeated_unit(self):
        before = state.inspect_state(self.root), self.fixture.fixture.fixture.fixture.path.read_bytes()
        self.assertIs(helpers.offline(self.root, self.proof), True)
        self.stopped.assert_called_once();self.quiet.assert_called_once()
        self.assertEqual((state.inspect_state(self.root), self.fixture.fixture.fixture.fixture.path.read_bytes()), before)
        self.actor['ActiveState'] = 'active'
        self.assertIs(helpers.offline(self.root, self.proof), False)
        self.actor['ActiveState'] = 'failed'
        self.quiet.side_effect = ControlError('orphaned descendant')
        with self.assertRaises(ControlError):helpers.offline(self.root, self.proof)

    def test_wait_only_observes_reviewed_new_invocation_and_cannot_reuse_replaced_permission(self):
        active = {'ActiveState': 'active', 'MainPID': str(self.fixture.main), 'InvocationID': 'c'*32}
        self.unit.side_effect = [self.actor, active]
        with patch.object(helpers.time, 'sleep') as sleep, patch.object(update_resumption_runtime, 'require_binding') as bind:
            helpers.wait(self.root, self.proof)
        sleep.assert_called_once_with(.2)
        bind.assert_called_once_with(self.root, 'c'*32,
            control_manifest_sha256=self.fixture.grant['control_manifest_sha256'])
        self.assertEqual(self.proof.require_current.call_count, 2)
        with patch.object(update_grants, 'read_grant', side_effect=[self.fixture.grant,
                {**self.fixture.grant, 'review_id': self.fixture.grant['installation_id']}]), self.assertRaises(ControlError):
            helpers.wait(self.root, self.proof)
