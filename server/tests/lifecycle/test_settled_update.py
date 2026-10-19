"""Terminal update evidence permits restart without resurrecting retired ownership."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from lifecycle import cli, state, update_controller


class SettledUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'installed'
        (self.root / 'server').mkdir(parents=True)
        for name in ('start.sh', 'update.sh'):
            (self.root / name).write_text('#!/bin/sh\n')
        self.git('init', '-q')
        (self.root / 'marker').write_text('previous')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'previous')
        self.previous = self.git('rev-parse', 'HEAD')
        (self.root / 'marker').write_text('target')
        self.git('add', 'marker')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'target')
        self.target = self.git('rev-parse', 'HEAD')
        (self.root / '.run').mkdir(mode=0o700)
        self.journal = self.root / '.run/update-state.json'
        self.payload = {
            'phase': 'succeeded', 'transaction_id': str(uuid.uuid4()),
            'previous_commit': self.previous, 'target_commit': self.target, 'current_commit': self.target,
            'runtime_committed': True, 'runtime_committed_at': 10.0, 'finished_at': 11.0,
            'lifecycle_generation': None, 'lifecycle_phase': None, 'error': '', 'failed_target': '',
        }
        self.write()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def write(self, **changes):
        self.journal.write_text(json.dumps({**self.payload, **changes}))
        self.journal.chmod(0o600)

    def command(self):
        source = Path(__file__).resolve().parents[2]
        return subprocess.run([sys.executable, '-m', 'lifecycle.cli', '--root', str(self.root), 'update-journal-settled'],
                              env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'},
                              text=True, capture_output=True, timeout=5)

    def canonical(self):
        claim = state.claim_operation_with_credential(self.root, operation_type='update', owner_kind='updater',
                                                     operation_id=self.payload['transaction_id'])
        for before, after in [('reserved', 'exclusive'), ('exclusive', 'verifying')]:
            state.transition_operation_with_credential(self.root, operation_id=self.payload['transaction_id'],
                generation=claim.state.generation, expected_phase=before, next_phase=after)
        self.payload.update(lifecycle_generation=claim.state.generation, lifecycle_phase='verifying')
        self.write()
        return claim

    def complete(self, claim):
        state.complete_operation_with_credential(self.root, operation_id=self.payload['transaction_id'],
                                                 generation=claim.state.generation, expected_phase='verifying')

    def test_legacy_and_completed_canonical_success_are_read_only(self):
        self.assertTrue(update_controller.terminal_update_journal_settled(self.root))
        self.assertFalse(state.lifecycle_paths(self.root).lifecycle_dir.exists())
        self.assertEqual(self.command().returncode, cli.EXIT_OK)
        claim = self.canonical()
        with self.assertRaises(update_controller.UpdateControllerError):
            update_controller.terminal_update_journal_settled(self.root)
        self.complete(claim)
        before = self.journal.read_bytes(), state.lifecycle_paths(self.root).state_path.read_bytes()
        self.assertTrue(update_controller.terminal_update_journal_settled(self.root))
        self.assertEqual(before, (self.journal.read_bytes(), state.lifecycle_paths(self.root).state_path.read_bytes()))
        self.assertEqual(self.command().returncode, cli.EXIT_OK)

    def test_accepted_previous_release_and_legacy_rollback_can_restart(self):
        claim = self.canonical()
        self.complete(claim)
        self.git('checkout', '--detach', self.previous)
        self.payload['current_commit'] = self.previous
        self.write()
        self.assertTrue(update_controller.terminal_update_journal_settled(self.root))
        self.write(phase='rolled_back', failed_target=self.target)
        self.assertTrue(update_controller.terminal_update_journal_settled(self.root))
        self.write(phase='rolled_back', failed_target=self.target, finished_at=9.0)
        # A phase written before protected commit is not post-recovery evidence.
        self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)

    def test_uncommitted_previous_legacy_release_has_no_shared_identity(self):
        self.git('checkout', '--detach', self.previous)
        self.write(phase='rolled_back', failed_target=self.target, current_commit=self.previous,
                   runtime_committed=False, runtime_committed_at=None)
        self.assertTrue(update_controller.terminal_update_journal_settled(self.root))
        self.assertFalse(state.lifecycle_paths(self.root).lifecycle_dir.exists())

    def test_terminal_disagreements_fail_closed_in_cli(self):
        for changes in (
            {'transaction_id': 'prefix'}, {'target_commit': 1}, {'current_commit': self.previous},
            {'runtime_committed': 1}, {'runtime_committed': False}, {'runtime_committed_at': None},
            {'finished_at': True}, {'finished_at': 0}, {'finished_at': 9.0},
            {'runtime_committed_at': float('inf')}, {'error': 'post_commit_recovery_failed'},
            {'failed_target': self.target}, {'lifecycle_generation': True}, {'lifecycle_generation': 1},
            {'lifecycle_phase': 'recovery_required'}, {'phase': []},
        ):
            with self.subTest(changes=changes):
                self.write(**changes)
                result = self.command()
                self.assertNotEqual(result.returncode, cli.EXIT_OK)
                self.assertNotEqual(result.returncode, cli.EXIT_CONFLICT)
        self.write(phase='starting')
        self.assertFalse(update_controller.terminal_update_journal_settled(self.root))
        self.assertEqual(self.command().returncode, cli.EXIT_CONFLICT)

    def test_strict_receipt_duplicate_fifo_symlink_and_corrupt_json_are_not_recovery(self):
        for raw in (
            '{', '[]', ' ' * (64 * 1024 + 1),
            json.dumps(self.payload).replace('"runtime_committed": true', '"runtime_committed": false, "runtime_committed": true'),
        ):
            self.journal.write_text(raw)
            self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)
        self.journal.unlink()
        os.mkfifo(self.journal)
        self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)
        self.journal.unlink()
        receipt = self.root / '.run/receipt'
        receipt.write_text(json.dumps(self.payload))
        self.journal.symlink_to(receipt)
        self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)

    def test_stale_generation_or_held_controller_lock_cannot_be_skipped(self):
        claim = self.canonical()
        self.complete(claim)
        lock = state.lifecycle_paths(self.root).controller_lock_path
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)
        finally:
            os.close(fd)
        self.assertTrue(update_controller.terminal_update_journal_settled(self.root))
        next_claim = state.claim_operation_with_credential(self.root, operation_type='restore', owner_kind='restore_controller')
        self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)
        state.complete_operation_with_credential(self.root, operation_id=next_claim.state.active_operation.operation_id,
                                                 generation=next_claim.state.generation, expected_phase='reserved')
        self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)

    def test_actual_shell_returns_no_recovery_only_for_strict_settled_receipt(self):
        repository = Path(__file__).resolve().parents[3]
        shutil.copytree(repository / 'server/lifecycle', self.root / 'server/lifecycle')
        source = (repository / 'update.sh').read_text().partition('case "${1:-}" in')[0]
        library = Path(self.temporary.name) / 'update-library.sh'
        library.write_text(source)
        script = 'source "$1"\nROOT_DIR="$2"\nrecover_interrupted_release\n'
        for phase, expected in [('succeeded', 10), ('succeeded', 1)]:
            self.write()
            if expected == 1:
                self.journal.write_text('{')
            result = subprocess.run(['bash', '-c', script, 'bash', str(library), str(self.root)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_live_or_foreign_controller_lease_prevents_normal_restart(self):
        claim = self.canonical()
        handle = state.acquire_controller_lease(
            self.root, operation_id=self.payload['transaction_id'], generation=claim.state.generation,
            expected_phase='verifying', owner_capability=claim.owner_capability,
        )
        lease_path = state.lifecycle_paths(self.root).lease_path
        lease_payload = json.loads(lease_path.read_text())
        try:
            self.complete(claim)
            self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)
        finally:
            handle.release()
        self.assertEqual(self.command().returncode, cli.EXIT_OK)
        lease_payload['operation_id'] = str(uuid.uuid4())
        lease_path.write_text(json.dumps(lease_payload))
        lease_path.chmod(0o600)
        self.assertEqual(self.command().returncode, cli.EXIT_CORRUPT_STATE)

    def test_normal_discovery_states_remain_no_recovery_in_actual_shell(self):
        repository = Path(__file__).resolve().parents[3]
        shutil.copytree(repository / 'server/lifecycle', self.root / 'server/lifecycle')
        library = Path(self.temporary.name) / 'update-library.sh'
        library.write_text((repository / 'update.sh').read_text().partition('case "${1:-}" in')[0])
        script = 'source "$1"\nROOT_DIR="$2"\nrecover_interrupted_release\n'
        for phase in ('idle', 'checking', 'up_to_date'):
            with self.subTest(phase=phase):
                self.journal.write_text(json.dumps({'phase': phase}))
                self.assertEqual(self.command().returncode, cli.EXIT_CONFLICT)
                result = subprocess.run(['bash', '-c', script, 'bash', str(library), str(self.root)],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 10, result.stderr)
