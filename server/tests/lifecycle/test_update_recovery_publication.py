"""Root OS publication flow; protected ownership/systemd effects explicitly substituted."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import update_recover as control, update_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_update_grants as grant_fixture


class UpdateRecoveryPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = grant_fixture.UpdateGrantTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.grant = fixture.prepare();self.operation = self.grant['operation_id']
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)/'operations'
        self.enterContext(patch.object(update_grants, 'ROOT', self.base))
        self.enterContext(patch.object(control, 'require_root'))
        self.enterContext(patch.object(control, 'descriptor', return_value=SimpleNamespace(
            installation_id=self.grant['installation_id'], service_gid=os.getgid())))
        self.unit = self.enterContext(patch.object(control, 'operation_unit', return_value={'ActiveState': 'inactive', 'MainPID': '0'}))
        self.empty = self.enterContext(patch('lifecycle.control.stop._require_empty'))
        self.start = self.enterContext(patch.object(control, 'systemctl'))
        self.result = self.enterContext(patch.object(control, 'run_worker', return_value={'completed': True}))
        def directory(path, **kwargs):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            return os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.enterContext(patch.object(control, 'root_directory', side_effect=directory))
        def write(path, value, **kwargs):
            path.write_text(json.dumps(value));path.chmod(kwargs['mode'])
        self.writer = self.enterContext(patch.object(control, 'atomic_root_json', side_effect=write))
        self.enterContext(patch('lifecycle.control.tickets.read_protected_json',
            side_effect=lambda path: json.loads(path.read_text())))

    def test_exact_metadata_is_retained_before_one_fixed_operation_unit_start(self):
        control._run(self.grant)
        path = update_grants.grant_path(self.operation, 3)
        retained = self.base/self.operation/('update-review-'+self.grant['review_id']+'.json')
        self.assertEqual(json.loads(path.read_text()), self.grant)
        self.assertEqual(json.loads(retained.read_text()), self.grant)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        name = 'streamhome-operation@'+self.operation+'.service'
        self.start.assert_called_once_with('start', name, timeout=30)
        self.assertEqual(self.empty.call_count, 2)
        self.result.assert_called_once_with(['update-recovery-result', self.operation],
            payload={'controller_generation': 3}, timeout=120)

    def test_busy_or_foreign_process_group_is_refused_before_permission_publication(self):
        self.unit.return_value = {'ActiveState': 'active', 'MainPID': '4321'}
        with self.assertRaises(ControlError):control._run(self.grant)
        self.writer.assert_not_called();self.start.assert_not_called()
        self.unit.return_value = {'ActiveState': 'inactive', 'MainPID': '0'}
        self.empty.side_effect = ControlError('control_children_busy')
        with self.assertRaises(ControlError):control._run(self.grant)
        self.writer.assert_not_called();self.start.assert_not_called()

    def test_changed_retained_review_is_preserved_and_never_dispatched(self):
        directory = self.base/self.operation;directory.mkdir(parents=True)
        path = directory/('update-review-'+self.grant['review_id']+'.json')
        path.write_text(json.dumps({**self.grant, 'source_evidence_sha256': 'f'*64}))
        before = path.read_bytes()
        with self.assertRaises(ControlError):control._run(self.grant)
        self.assertEqual(path.read_bytes(), before);self.start.assert_not_called()

    def test_other_operation_permission_cannot_be_overwritten_by_matching_filename(self):
        directory = self.base/self.operation;directory.mkdir(parents=True)
        path = update_grants.grant_path(self.operation, 3)
        old = {**self.grant, 'operation_id': str(uuid.uuid4())}
        path.write_text(json.dumps(old));before = path.read_bytes()
        with self.assertRaises(ControlError):control._run(self.grant)
        self.assertEqual(path.read_bytes(), before);self.start.assert_not_called()

    def test_permission_replacement_after_dispatch_never_restarts_or_reports_completion(self):
        def replace(*args, **kwargs):
            path = update_grants.grant_path(self.operation, 3)
            path.write_text(json.dumps({**self.grant, 'source_evidence_sha256': 'f'*64}))
        self.start.side_effect = replace
        with self.assertRaises(ControlError):control._run(self.grant)
        self.assertEqual(self.start.call_count, 1);self.result.assert_not_called()

    def test_bound_repair_starts_only_fixed_update_owner_and_reads_maintenance_result_without_completion(self):
        grant = {**self.grant, 'schema_version': 3, 'action': 'repair_bound_environment',
            'runtime_status': 'offline', 'filesystem_sha256': '7'*64,
            'source_journal_sha256': '8'*64, 'source_journal_generation': self.grant['generation'],
            'repair_execution_sha256': self.grant['accepted_execution_sha256']}
        grant.pop('canonical_evidence_identity')
        with patch.object(control, 'require_stopped') as stopped:
            control._run(grant)
        stopped.assert_called_once()
        self.start.assert_called_once_with('start', 'streamhome-operation@'+self.operation+'.service', timeout=30)
        self.result.assert_called_once_with(['bound-repair-result', self.operation], timeout=180)
