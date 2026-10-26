"""UID worker routing/finite packets; protected reader and OS proof explicitly substituted."""
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import state, update_recovery
from lifecycle.control import workers
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture


class UpdateRecoveryWorkerTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.root = fixture.root;self.operation = fixture.fixture.operation
        self.enterContext(patch.object(workers, 'ROOT', self.root))
        self.enterContext(patch.object(workers, 'inspect_installation', return_value=fixture.fixture.fixture.layout.descriptor))

    def invoke(self, arguments, payload):
        output = io.StringIO()
        with patch.object(workers.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), redirect_stdout(output):
            code = workers.main(arguments, bundle=self.root, manifest={}, manifest_sha256=self.fixture.fixture.digest)
        return code, json.loads(output.getvalue())

    def test_original_update_review_uses_update_reader_without_mutation(self):
        before = state.inspect_state(self.root)
        code, result = self.invoke(['recovery-review', self.operation], {})
        self.assertEqual(code, 0);self.assertEqual(result['operation_type'], 'update')
        self.fixture.review.assert_called_with(self.root, self.operation,
            control_manifest_sha256=self.fixture.fixture.digest)
        self.assertEqual(state.inspect_state(self.root), before)

    def test_preparation_returns_permission_only_and_refuses_conflicting_positional_operation(self):
        before = state.inspect_state(self.root)
        code, grant = self.invoke(['prepare-update-recovery'], self.fixture.payload)
        self.assertEqual(code, 0);self.assertEqual(grant['operation_id'], self.operation)
        self.assertEqual(grant['generation'], 2)
        with self.assertRaises(ControlError):
            self.invoke(['prepare-update-recovery', self.operation], self.fixture.payload)
        self.assertEqual(state.inspect_state(self.root), before)

    def test_result_requires_exact_operation_and_typed_generation_packet(self):
        with patch.object(update_recovery, 'result', return_value={'completed': True}) as result:
            code, value = self.invoke(['update-recovery-result', self.operation], {'controller_generation': 3})
            self.assertEqual((code, value), (0, {'completed': True}))
            result.assert_called_once_with(self.root, self.operation, 3,
                control_manifest_sha256=self.fixture.fixture.digest)
            result.reset_mock()
            for payload in ({'controller_generation': True}, {'controller_generation': 1},
                    {'controller_generation': 3, 'extra': 1}, {'controller_generation': 2**63}):
                with self.subTest(payload=payload), self.assertRaises(ControlError):
                    self.invoke(['update-recovery-result', self.operation], payload)
            with self.assertRaises(ControlError):self.invoke(['update-recovery-result'], {'controller_generation': 3})
            result.assert_not_called()

    def test_unresolved_update_requires_consumed_start_intent_before_new_supervisor(self):
        before = state.inspect_state(self.root)
        self.assertEqual(self.invoke(['resumption-required'], {}), (0, {'required': True}))
        self.assertEqual(state.inspect_state(self.root), before)
