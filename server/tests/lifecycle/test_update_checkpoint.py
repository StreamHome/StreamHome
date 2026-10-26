"""Actual SQLite/WAL and canonical checkpoint binding; supervisor OS effect substituted."""
import json
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from lifecycle import execution_state, managed_update, state, update_checkpoint, update_controller, update_execution
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from tests.lifecycle.test_update_delegation import UpdateDelegateFixture


class ManagedUpdateCheckpointTests(UpdateDelegateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.layout.state.chmod(0o700)
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(self.root), *args],
                stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q');git('add', 'start.sh', 'update.sh')
        git('-c', 'user.name=Checkpoint fixture', '-c', 'user.email=checkpoint@example.invalid',
            'commit', '-qm', 'Synthetic original source')
        previous = git('rev-parse', 'HEAD')
        payload = update_controller.read_update_journal(self.path)
        payload.update(previous_commit=previous, current_commit=previous)
        payload['managed_execution']['execution'].update(application_commit=previous, application_build_id=previous[:12])
        self.path.write_text(json.dumps(payload));self.records[self.shell]['arguments'][4] = previous.encode()
        self.parent = update_execution.parse(payload).parent
        candidate = managed_update.prepared_execution(self.parent.execution, payload['target_commit'], self.built)
        payload['managed_candidate'] = execution_tuple_payload(candidate)
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='reserved', next_phase='exclusive')
        payload.update(lifecycle_phase='exclusive', phase='stopping');self.path.write_text(json.dumps(payload))
        self.enterContext(patch.object(update_checkpoint, 'layout_for_root', return_value=self.layout))
        self.enterContext(patch.object(execution_state, 'layout_for_root', return_value=self.layout))
        state._atomic_write_json(self.layout.state/'active-runtime.json', execution_state.ExecutionRecord(
            self.parent.installation_id, self.parent.execution, self.operation, 0).payload())
        self.stopped = self.enterContext(patch.object(managed_update, 'supervisor_command', return_value={'stopped': True}))
        self.database = self.layout.database
        self.connection = sqlite3.connect(self.database);self.addCleanup(self.connection.close)
        self.database.chmod(0o600)
        self.connection.execute('PRAGMA journal_mode=WAL')
        from lifecycle.restore_controller import REQUIRED_TABLES
        for table in sorted(REQUIRED_TABLES):
            self.connection.execute('CREATE TABLE '+table+' (id INTEGER PRIMARY KEY)')
        self.connection.execute('CREATE TABLE checkpoint_sentinel (value TEXT)')
        self.connection.execute("INSERT INTO checkpoint_sentinel VALUES ('original durable data')")
        self.connection.commit()

    def create(self):return update_checkpoint.create(self.root, self.proof())

    def bound(self):return update_execution.parse(update_controller.read_update_journal(self.path))

    def test_failed_rollback_progress_requires_fresh_transferred_continuation_permission(self):
        self.enterContext(patch.object(update_controller, 'layout_for_root', return_value=self.layout))
        self.create()
        payload = update_controller.read_update_journal(self.path)
        bound = self.bound()
        payload.update(phase='rollback_failed', failed_target=payload['target_commit'],
            managed_verification_execution_sha256=execution_tuple_sha256(bound.candidate),
            lifecycle_phase='recovery_required')
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='exclusive', next_phase='recovery_required')
        self.path.write_text(json.dumps(payload))
        self.lease.release()
        capability = state.load_controller_capability(self.root, operation_id=self.operation, generation=2)
        with patch.object(state.os, 'getpid', return_value=self.main):
            lease, transferred = state.acquire_transferred_controller_lease(self.root, operation_id=self.operation,
                generation=2, expected_phase='recovery_required', owner_capability=capability, next_owner_kind='updater')
        self.addCleanup(lease.release)
        self.assertEqual(transferred.state.generation, 3)
        payload.update(lifecycle_generation=3)
        self.path.write_text(json.dumps(payload))
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=3,
            expected_phase='recovery_required', next_phase='exclusive')
        before = self.path.read_bytes()
        with patch('lifecycle.update_recovery.require_controller_permission') as permission:
            for grant in (None, {'action': 'repair_bound_environment'}, {'action': 'safe_abort'}):
                permission.return_value = grant
                with self.subTest(grant=grant), self.assertRaises(update_controller.UpdateControllerConflict):
                    update_controller.mutate_update_journal(self.root, operation_id=self.operation,
                        generation=3, expected_lifecycle_phase='exclusive', changes={'phase': 'recovering'})
                self.assertEqual(self.path.read_bytes(), before)
            permission.return_value = {'action': 'continue_recovery'}
            result = update_controller.mutate_update_journal(self.root, operation_id=self.operation,
                generation=3, expected_lifecycle_phase='exclusive', changes={'phase': 'recovering'})
            self.assertEqual(result['phase'], 'recovering')
            self.assertFalse(result['runtime_committed'])

    def accept(self, *, previous=False):
        payload = update_controller.read_update_journal(self.path)
        bound = update_execution.parse(payload);selected = bound.parent.execution if previous else bound.candidate
        state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
            expected_phase='exclusive', next_phase='verifying')
        payload.update(lifecycle_phase='verifying', phase='recovering', runtime_committed=True, runtime_committed_at=1,
            current_commit=selected.application_commit,
            managed_accepted_execution_sha256=execution_tuple_sha256(selected),
            managed_verification_execution_sha256=execution_tuple_sha256(selected))
        self.path.write_text(json.dumps(payload))
        state._atomic_write_json(self.layout.state/'active-runtime.json', execution_state.ExecutionRecord(
            self.parent.installation_id, selected, self.operation, 2).payload())

    def test_guarded_launch_requires_bound_snapshot_and_existing_canonical_database(self):
        with self.assertRaises(ControlError):update_checkpoint.require_launch_database(self.root, self.bound())
        self.create()
        self.assertEqual(update_checkpoint.require_launch_database(self.root, self.bound()), self.database)
        self.accept()
        update_checkpoint.path_for(self.root, self.operation).unlink()
        self.assertEqual(update_checkpoint.require_launch_database(self.root, self.bound()), self.database)
        self.connection.close();self.database.unlink()
        with self.assertRaises(FileNotFoundError):update_checkpoint.require_launch_database(self.root, self.bound())
        self.assertFalse(self.database.exists())

    def test_original_rollback_replaces_committed_candidate_wal_with_bound_snapshot(self):
        self.create();self.connection.close()
        # Abrupt SQLite writer exit retains real committed WAL without a live reader.
        subprocess.run([sys.executable, '-c',
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); "
            "c.execute(\"UPDATE checkpoint_sentinel SET value='candidate data'\"); c.commit(); os._exit(0)",
            str(self.database)], check=True)
        self.assertTrue(self.database.with_name(self.database.name+'-wal').is_file())
        payload = update_controller.read_update_journal(self.path)
        payload.update(phase='rolling_back', failed_target=payload['target_commit']);self.path.write_text(json.dumps(payload))
        before = state.inspect_state(self.root), self.path.read_bytes()
        with patch('lifecycle.restore_controller.require_no_media_publication_evidence') as media:
            self.assertEqual(update_checkpoint.restore(self.root, self.proof()), {'restored': True})
        media.assert_called_once_with(self.root)
        connection = sqlite3.connect(self.database)
        try:self.assertEqual(connection.execute('SELECT value FROM checkpoint_sentinel').fetchall(), [('original durable data',)])
        finally:connection.close()
        self.assertEqual((state.inspect_state(self.root), self.path.read_bytes()), before)
        self.assertFalse(self.database.with_name(self.database.name+'-wal').exists())

    def test_accepted_boundary_refuses_database_rollback_and_preserves_snapshot(self):
        self.create();self.accept()
        checkpoint = update_checkpoint.path_for(self.root, self.operation)
        before = checkpoint.read_bytes(), self.database.read_bytes(), self.path.read_bytes()
        with self.assertRaises(ControlError):update_checkpoint.restore(self.root, self.proof())
        self.assertEqual((checkpoint.read_bytes(), self.database.read_bytes(), self.path.read_bytes()), before)

    def test_loss_of_quiescence_after_copy_cannot_publish_old_database(self):
        from lifecycle import restore_controller
        self.create()
        self.connection.execute("UPDATE checkpoint_sentinel SET value='candidate data'")
        self.connection.commit();self.connection.close()
        payload = update_controller.read_update_journal(self.path)
        payload.update(phase='rolling_back', failed_target=payload['target_commit'])
        self.path.write_text(json.dumps(payload));original = self.path.read_bytes()
        copy = restore_controller._copy_sqlite
        def interrupted(*args, **kwargs):
            publish = kwargs['before_publish']
            def stopped_before_publication():
                self.stopped.return_value = {'stopped': False}
                publish()
            return copy(*args, **{**kwargs, 'before_publish': stopped_before_publication})
        with patch.object(restore_controller, '_copy_sqlite', side_effect=interrupted), patch.object(
                restore_controller, 'require_no_media_publication_evidence'):
            with self.assertRaises(ControlError):update_checkpoint.restore(self.root, self.proof())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM checkpoint_sentinel').fetchall(), [('candidate data',)])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNotNone(self.bound().checkpoint)

    def test_cleanup_preserves_unaccepted_or_changed_snapshot_and_requires_actual_readiness(self):
        self.create();checkpoint = update_checkpoint.path_for(self.root, self.operation)
        self.assertEqual(update_checkpoint.cleanup(self.root, self.proof()), {'discarded': False})
        self.accept()
        with patch.object(managed_update, 'runtime_action', return_value={'ready': False}):
            self.assertEqual(update_checkpoint.cleanup(self.root, self.proof()), {'discarded': False})
        raw = checkpoint.read_bytes();checkpoint.write_bytes(raw+b'unknown evidence')
        with patch.object(managed_update, 'runtime_action', return_value={'ready': True}):
            self.assertEqual(update_checkpoint.cleanup(self.root, self.proof()), {'discarded': False})
            self.assertEqual(checkpoint.read_bytes(), raw+b'unknown evidence')
            checkpoint.write_bytes(raw)
            self.assertEqual(update_checkpoint.cleanup(self.root, self.proof()), {'discarded': True})
        self.assertFalse(checkpoint.exists());self.assertIsNotNone(self.bound().checkpoint)

    def test_recovery_evidence_uses_only_its_bound_checkpoint_and_rejects_unknown_artifacts(self):
        from lifecycle import recovery
        with patch.object(recovery, 'layout_for_root', return_value=self.layout):
            self.assertEqual(recovery._update_checkpoint_status(self.root, update_controller.read_update_journal(self.path)), 'absent')
            self.create();payload = update_controller.read_update_journal(self.path)
            checkpoint = update_checkpoint.path_for(self.root, self.operation)
            self.assertEqual(recovery._update_checkpoint_path(self.root, payload), checkpoint)
            self.assertEqual(recovery._update_checkpoint_status(self.root, payload), 'valid')
            old = recovery._durable_recovery_evidence(self.root, self.operation, state.OperationType.UPDATE)
            legacy = self.layout.durable_runtime/'pre-update-database.db'
            legacy.write_bytes(b'Unrelated retained evidence');legacy.chmod(0o600)
            self.assertEqual(recovery._durable_recovery_evidence(self.root, self.operation, state.OperationType.UPDATE), old)
            checkpoint.write_bytes(checkpoint.read_bytes()+b'changed')
            self.assertEqual(recovery._update_checkpoint_status(self.root, payload), 'invalid')
            self.assertNotEqual(recovery._durable_recovery_evidence(self.root, self.operation, state.OperationType.UPDATE), old)
            checkpoint.unlink()
            self.assertEqual(recovery._update_checkpoint_status(self.root, payload), 'invalid')

    def test_committed_wal_is_captured_without_changing_canonical_data_or_legacy_checkpoint(self):
        legacy = self.path.parent/'pre-update-database.db';legacy.write_bytes(b'Unrelated retained legacy evidence')
        legacy.chmod(0o600);canonical = self.database.read_bytes()
        authority = state.inspect_state(self.root)
        self.assertEqual(self.create(), {'created': True})
        payload = update_controller.read_update_journal(self.path);bound = update_execution.parse(payload)
        checkpoint = update_checkpoint.require_checkpoint(self.root, bound)
        with closing(sqlite3.connect(checkpoint)) as snapshot:
            self.assertEqual(snapshot.execute('SELECT value FROM checkpoint_sentinel').fetchall(), [('original durable data',)])
        self.assertEqual(self.database.read_bytes(), canonical)
        self.assertEqual(legacy.read_bytes(), b'Unrelated retained legacy evidence')
        self.assertEqual(state.inspect_state(self.root), authority)
        self.assertEqual(bound.checkpoint.operation_id, self.operation)
        self.assertEqual(bound.checkpoint.generation, 2)
        self.assertIsNone(bound.accepted)
        saved = checkpoint.read_bytes(), self.path.read_bytes()
        with self.assertRaises(ControlError):self.create()
        self.assertEqual((checkpoint.read_bytes(), self.path.read_bytes()), saved)

    def test_preexisting_unbound_operation_file_is_preserved_not_attributed_by_sqlite_validity(self):
        checkpoint = update_checkpoint.path_for(self.root, self.operation, create=True)
        with closing(sqlite3.connect(checkpoint)) as connection:
            connection.execute('CREATE TABLE foreign_evidence (value TEXT)');connection.commit()
        checkpoint.chmod(0o600);before = checkpoint.read_bytes(), self.path.read_bytes()
        with self.assertRaisesRegex(ControlError, 'control_update_checkpoint_exists'):self.create()
        self.assertEqual((checkpoint.read_bytes(), self.path.read_bytes()), before)

    def test_canonical_alias_or_unknown_quiescence_cannot_create_or_bind_a_checkpoint(self):
        original = self.path.read_bytes()
        self.stopped.return_value = {'stopped': False}
        with self.assertRaises(ControlError):self.create()
        self.assertEqual(self.path.read_bytes(), original)
        self.stopped.return_value = {'stopped': True}
        self.connection.close()
        moved = self.database.with_name('retained-original.db');self.database.rename(moved)
        self.database.symlink_to(moved)
        with self.assertRaises(ControlError):self.create()
        self.assertEqual(self.path.read_bytes(), original)

    def test_modified_hardlinked_or_wrong_scope_checkpoint_cannot_authorize_restore(self):
        self.create();bound = update_execution.parse(update_controller.read_update_journal(self.path))
        checkpoint = update_checkpoint.require_checkpoint(self.root, bound)
        alias = checkpoint.with_name('alias.db');os.link(checkpoint, alias)
        with self.assertRaises(ControlError):update_checkpoint.require_checkpoint(self.root, bound)
        alias.unlink();raw = checkpoint.read_bytes();checkpoint.write_bytes(raw+b'changed')
        with self.assertRaises(ControlError):update_checkpoint.require_checkpoint(self.root, bound)
        checkpoint.write_bytes(raw)
        self.assertEqual(update_checkpoint.require_checkpoint(self.root, bound), checkpoint)
        changed = bound.checkpoint.payload();changed['generation'] = 3
        with self.assertRaises(ValueError):update_execution.parse({**update_controller.read_update_journal(self.path),
            'managed_database_checkpoint': changed})

    def test_journal_publication_failure_preserves_original_data_and_unbound_checkpoint_evidence(self):
        original = self.path.read_bytes();canonical = self.database.read_bytes()
        with patch.object(update_controller, '_private_atomic_write', side_effect=OSError('Injected checkpoint journal failure')):
            with self.assertRaises(OSError):self.create()
        self.assertEqual(self.path.read_bytes(), original);self.assertEqual(self.database.read_bytes(), canonical)
        checkpoint = update_checkpoint.path_for(self.root, self.operation)
        self.assertTrue(checkpoint.is_file())
        with self.assertRaisesRegex(ControlError, 'control_update_checkpoint_exists'):self.create()
