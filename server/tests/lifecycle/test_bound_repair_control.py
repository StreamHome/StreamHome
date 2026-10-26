from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import administration, bound_repair
from lifecycle.control.rpc import ControlError


class BoundRepairRootControlTests(unittest.TestCase):
    def test_exact_decline_or_stale_review_never_implies_parent_continuation(self):
        snapshot = {'operation_id': str(uuid.uuid4()), 'generation': 2, 'evidence_sha256': 'a' * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            value = {'schema_version': 1, 'operation_id': snapshot['operation_id'], 'expected_generation': 2,
                'expected_evidence_sha256': 'a' * 64, 'repair_bound_environment': False}
            path.write_text(json.dumps(value));self.assertFalse(bound_repair._consent(path, snapshot))
            value['repair_bound_environment'] = True;path.write_text(json.dumps(value))
            self.assertTrue(bound_repair._consent(path, snapshot))
            value['expected_generation'] = 1;path.write_text(json.dumps(value))
            with self.assertRaises(ControlError):bound_repair._consent(path, snapshot)
            value.update(expected_generation=2, continue_recovery=True);path.write_text(json.dumps(value))
            with self.assertRaises(ControlError):bound_repair._consent(path, snapshot)

    def test_root_bridge_never_changes_boot_policy_or_starts_runtime_for_a_maintenance_grant(self):
        operation = str(uuid.uuid4());review = str(uuid.uuid4())
        payload = {'operation_id': operation, 'expected_generation': 2}
        grant = {'schema_version': 4, 'recovery_review_id': review, 'request': payload}
        inactive = {'ActiveState': 'inactive', 'MainPID': '0', 'InvocationID': ''}
        active = {'ActiveState': 'active', 'MainPID': '200', 'InvocationID': '1' * 32}
        observation = {'stage': 'recovery_required', 'os_effect_requested': True}
        result = {'completed': True, 'stage': 'environment_repaired_recovery_required'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / operation).mkdir()
            with patch.object(administration, 'OPERATIONS', root), patch.object(administration, 'require_root'), patch.object(administration, 'descriptor', return_value=SimpleNamespace(service_gid=0)), patch.object(administration, 'operation_unit', side_effect=[inactive, active, inactive]), patch('lifecycle.control.stop._require_empty'), patch.object(administration, 'atomic_root_json', side_effect=lambda path, value, **kwargs: path.write_text(json.dumps(value))), patch.object(administration, 'run_worker', side_effect=[observation, result]) as worker, patch.object(administration, '_apply_fixed_boot_effect') as effect, patch.object(administration, 'systemctl') as systemctl, patch.object(administration.time, 'monotonic', side_effect=[0, 310, 311]), patch.object(administration.time, 'sleep'), redirect_stdout(io.StringIO()):
                self.assertEqual(administration.run_operation(payload, start=False, recovery_grant=grant), result)
            effect.assert_not_called();systemctl.assert_not_called()
            self.assertEqual(worker.call_args.args[0], ['bound-repair-result', operation])

    def test_restore_repair_uses_original_parent_bridge_and_never_management_claim(self):
        operation=str(uuid.uuid4());installation=str(uuid.uuid4());epoch='b'*64;manifest='c'*64
        snapshot={'operation_type':'restore','operation_id':operation,'installation_id':installation,
            'generation':3,'evidence_sha256':'a'*64}
        grant={'schema_version':3,'action':'repair_bound_environment','control_manifest_sha256':manifest}
        with patch.object(bound_repair,'require_root'),patch.object(bound_repair,'operation_unit',
            return_value={'ActiveState':'inactive','MainPID':'0'}),patch('lifecycle.control.stop._require_empty'),patch.object(
            bound_repair,'require_stopped'),patch.object(bound_repair,'root_stop_epoch',return_value=epoch),patch.object(
            bound_repair,'current_scope',return_value={'unit':'verified root scope'}),patch.object(
            bound_repair,'run_worker',side_effect=[snapshot,snapshot,grant]) as worker,patch.object(
            bound_repair,'_consent',return_value=True),patch('lifecycle.control.parent_recover._run') as parent,patch.object(
            bound_repair,'run_operation') as management,redirect_stdout(io.StringIO()):
            self.assertEqual(bound_repair.repair(operation,request_path=Path('/synthetic-reviewed-request'),
                control_manifest_sha256=manifest,scope_epoch=epoch),0)
        parent.assert_called_once_with(grant);management.assert_not_called()
        self.assertEqual(worker.call_args.args[0],['bound-repair-prepare'])

    def test_root_bound_repair_imports_no_canonical_or_application_modules(self):
        code = "import sys;import lifecycle.control.bound_repair;assert not {'lifecycle.state','lifecycle.management_journal','config','application','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_repair_uses_existing_update_owner_without_management_or_runtime_start(self):
        operation = str(uuid.uuid4());installation = str(uuid.uuid4());epoch = 'b'*64;manifest = 'c'*64
        snapshot = {'operation_type': 'update', 'operation_id': operation, 'installation_id': installation,
            'generation': 3, 'evidence_sha256': 'a'*64}
        grant = {'schema_version': 3, 'action': 'repair_bound_environment', 'control_manifest_sha256': manifest}
        with (patch.object(bound_repair, 'require_root'), patch.object(bound_repair, 'operation_unit',
                return_value={'ActiveState': 'inactive', 'MainPID': '0'}), patch('lifecycle.control.stop._require_empty'),
                patch.object(bound_repair, 'require_stopped'), patch.object(bound_repair, 'root_stop_epoch', return_value=epoch),
                patch.object(bound_repair, 'current_scope', return_value={'unit': 'verified root scope'}),
                patch.object(bound_repair, 'run_worker', side_effect=[snapshot, snapshot, grant]),
                patch.object(bound_repair, '_consent', return_value=True),
                patch('lifecycle.control.update_recover._run') as parent,
                patch.object(bound_repair, 'run_operation') as management, redirect_stdout(io.StringIO())):
            self.assertEqual(bound_repair.repair(operation, request_path=Path('/synthetic-reviewed-request'),
                control_manifest_sha256=manifest, scope_epoch=epoch), 0)
        parent.assert_called_once_with(grant);management.assert_not_called()


if __name__ == '__main__':unittest.main()
