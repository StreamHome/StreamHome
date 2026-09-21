from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from lifecycle import state


CAPABILITY_ONE = "a" * 43
CAPABILITY_TWO = "b" * 43


def _make_installation(root: Path) -> None:
    (root / "server").mkdir(parents=True)
    for name in ("start.sh", "update.sh"):
        (root / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")


def _claim_worker(root: str, operation_id: str, capability: str, queue: multiprocessing.Queue) -> None:
    try:
        claimed = state.claim_operation(
            root,
            operation_type=state.OperationType.UPDATE,
            owner_kind=state.OwnerKind.UPDATER,
            operation_id=operation_id,
            owner_capability=capability,
        )
        queue.put(("won", claimed.state.generation))
    except state.LifecycleConflictError:
        queue.put(("conflict", None))


def _hold_controller_lock(
    root: str,
    operation_id: str,
    capability: str,
    ready: multiprocessing.Event,
    stop: multiprocessing.Event,
) -> None:
    with state.acquire_controller_lease(
        root,
        operation_id=operation_id,
        generation=1,
        expected_phase="reserved",
        owner_capability=capability,
    ):
        ready.set()
        stop.wait(timeout=30)


class LifecycleStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "StreamHome"
        self.root.mkdir()
        _make_installation(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _claim(
        self,
        *,
        operation_id: str | None = None,
        capability: str = CAPABILITY_ONE,
    ) -> state.ClaimResult:
        return state.claim_operation(
            self.root,
            operation_type=state.OperationType.UPDATE,
            owner_kind=state.OwnerKind.UPDATER,
            operation_id=operation_id or str(uuid.uuid4()),
            owner_capability=capability,
        )

    def _write_raw_state(self, payload: object, *, text: str | None = None) -> Path:
        paths = state.lifecycle_paths(self.root)
        paths.run_dir.mkdir(mode=0o700, exist_ok=True)
        paths.lifecycle_dir.mkdir(mode=0o700, exist_ok=True)
        paths.state_path.write_text(
            text if text is not None else json.dumps(payload),
            encoding="utf-8",
        )
        paths.state_path.chmod(0o600)
        return paths.state_path

    def _prime_lifecycle_filesystem_qualification(self) -> None:
        paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(paths, credentials=True)
        state.qualify_local_filesystem(
            paths.lifecycle_dir,
            require_private=True,
            description="lifecycle authority directory",
        )

    def test_import_does_not_load_application_modules(self) -> None:
        import sys

        self.assertNotIn("config", sys.modules)
        self.assertNotIn("db", sys.modules)
        self.assertNotIn("main", sys.modules)
        self.assertFalse(any(name.startswith("services.") for name in sys.modules))

    def test_explicit_root_rejects_missing_markers_symlinks_and_traversal(self) -> None:
        unrelated = Path(self.temporary.name) / "unrelated"
        unrelated.mkdir()
        with self.assertRaises(state.InvalidRootError):
            state.inspect_state(unrelated)
        linked = Path(self.temporary.name) / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(state.InvalidRootError):
            state.inspect_state(linked)
        traversed = self.root / "server" / ".."
        with self.assertRaises(state.InvalidRootError):
            state.inspect_state(traversed)

    def test_missing_state_is_idle_generation_zero_without_creating_run_state(self) -> None:
        current = state.inspect_state(self.root)
        self.assertEqual(current, state.LifecycleState(1, 0, None))
        self.assertFalse((self.root / ".run").exists())

    def test_filesystem_qualification_accepts_supported_local_fixture(self) -> None:
        filesystem_type = state.qualify_local_filesystem(
            self.root,
            require_private=False,
            description="test installation",
        )
        self.assertIsInstance(filesystem_type, str)
        self.assertTrue(filesystem_type)

    def test_filesystem_qualification_rejects_symlink_and_unsupported_mount(self) -> None:
        linked = Path(self.temporary.name) / "filesystem-link"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(state.UnsupportedFilesystemError):
            state.qualify_local_filesystem(linked, require_private=False)
        with patch.object(
            state,
            "_filesystem_mount_identity",
            return_value=("1", "0", "0:1", "/", "/", "rw", "", "nfs4", "server", "rw"),
        ):
            with self.assertRaises(state.UnsupportedFilesystemError):
                state.qualify_local_filesystem(self.root, require_private=False)

    def test_filesystem_qualification_cache_is_exact_to_path_mount_and_options(self) -> None:
        sibling = Path(self.temporary.name) / "other"
        sibling.mkdir()
        identity_one = (
            "101", "1", "0:1", "/", str(self.root), "rw", "", "ext4", "/dev/test", "rw"
        )
        identity_two = (
            "102", "1", "0:1", "/", str(sibling), "rw", "", "ext4", "/dev/test", "rw"
        )
        identity_remounted = (
            "101", "1", "0:1", "/", str(self.root), "ro", "", "ext4", "/dev/test", "ro"
        )
        with (
            patch.object(
                state,
                "_filesystem_mount_identity",
                side_effect=(identity_one, identity_two, identity_remounted),
            ),
            patch.object(state, "_probe_filesystem_primitives") as probe,
        ):
            state.qualify_local_filesystem(self.root, require_private=False)
            state.qualify_local_filesystem(sibling, require_private=False)
            state.qualify_local_filesystem(self.root, require_private=False)
        self.assertEqual(probe.call_count, 3)

        linked = Path(self.temporary.name) / "linked-other"
        linked.symlink_to(sibling, target_is_directory=True)
        with self.assertRaises(state.UnsupportedFilesystemError):
            state.qualify_local_filesystem(linked, require_private=False)

    def test_filesystem_probe_fails_closed_for_flock_file_and_directory_fsync(self) -> None:
        with patch.object(
            state.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1),
        ):
            with self.assertRaises(state.UnsupportedFilesystemError):
                state._probe_filesystem_primitives(self.root)
        with patch.object(state.os, "fsync", side_effect=OSError("file fsync failed")):
            with self.assertRaises(state.UnsupportedFilesystemError):
                state._probe_filesystem_primitives(self.root)
        with patch.object(
            state,
            "_fsync_directory",
            side_effect=OSError("directory fsync failed"),
        ):
            with self.assertRaises(state.UnsupportedFilesystemError):
                state._probe_filesystem_primitives(self.root)

    def test_import_does_not_run_active_filesystem_probe(self) -> None:
        script = (
            "from pathlib import Path; import lifecycle.state; "
            "print(int(Path('.run').exists()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "0")

    def test_update_restore_exclusion_persists_through_recovery_required(self) -> None:
        claimed = self._claim()
        operation = claimed.state.active_operation
        with self.assertRaises(state.LifecycleConflictError):
            state.claim_operation_with_credential(
                self.root,
                operation_type="restore",
                owner_kind="restore_controller",
            )
        state.transition_operation(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
            owner_capability=CAPABILITY_ONE,
        )
        with self.assertRaises(state.LifecycleConflictError):
            state.claim_operation_with_credential(
                self.root,
                operation_type="restore",
                owner_kind="restore_controller",
            )

    def test_inspect_existing_state_does_not_create_a_lock_file(self) -> None:
        path = self._write_raw_state(
            {"schema_version": 1, "generation": 4, "active_operation": None}
        )
        self.assertEqual(state.inspect_state(self.root).generation, 4)
        self.assertFalse(path.with_name("state.lock").exists())

    def test_claim_complete_and_next_claim_preserve_monotonic_generation(self) -> None:
        first = self._claim()
        self.assertEqual(first.state.generation, 1)
        self.assertEqual(first.state.active_operation.phase, state.LifecyclePhase.RESERVED)
        completed = state.complete_operation(
            self.root,
            operation_id=first.state.active_operation.operation_id,
            generation=1,
            expected_phase=state.LifecyclePhase.RESERVED,
            owner_capability=CAPABILITY_ONE,
        )
        self.assertIsNone(completed.active_operation)
        self.assertEqual(completed.generation, 1)
        second = self._claim(capability=CAPABILITY_TWO)
        self.assertEqual(second.state.generation, 2)

    def test_concurrent_claim_has_exactly_one_winner(self) -> None:
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        processes = [
            context.Process(
                target=_claim_worker,
                args=(str(self.root), str(uuid.uuid4()), capability, queue),
            )
            for capability in (CAPABILITY_ONE, CAPABILITY_TWO)
        ]
        for process in processes:
            process.start()
        results = [queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([result[0] for result in results].count("won"), 1)
        self.assertEqual([result[0] for result in results].count("conflict"), 1)

    def test_idle_authority_guard_serializes_a_concurrent_claim(self) -> None:
        started = threading.Event()
        completed = threading.Event()

        def claim() -> None:
            started.set()
            state.claim_operation_with_credential(
                self.root,
                operation_type=state.OperationType.UPDATE,
                owner_kind=state.OwnerKind.UPDATER,
            )
            completed.set()

        with state.hold_idle_authority(self.root):
            worker = threading.Thread(target=claim)
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(completed.wait(timeout=0.05))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(completed.is_set())
        current = state.inspect_state(self.root)
        self.assertEqual(current.active_operation.operation_type, state.OperationType.UPDATE)

    def test_wrong_identity_generation_owner_and_phase_fail_closed(self) -> None:
        claimed = self._claim()
        operation = claimed.state.active_operation
        attempts = (
            {
                "operation_id": str(uuid.uuid4()),
                "generation": 1,
                "expected_phase": "reserved",
                "owner_capability": CAPABILITY_ONE,
            },
            {
                "operation_id": operation.operation_id,
                "generation": 2,
                "expected_phase": "reserved",
                "owner_capability": CAPABILITY_ONE,
            },
            {
                "operation_id": operation.operation_id,
                "generation": 1,
                "expected_phase": "exclusive",
                "owner_capability": CAPABILITY_ONE,
            },
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaises(state.LifecycleConflictError):
                state.complete_operation(self.root, **attempt)
        with self.assertRaises(state.OwnershipError):
            state.complete_operation(
                self.root,
                operation_id=operation.operation_id,
                generation=1,
                expected_phase="reserved",
                owner_capability=CAPABILITY_TWO,
            )
        self.assertIsNotNone(state.inspect_state(self.root).active_operation)

    def test_same_owner_transition_retains_generation_and_stale_callback_fails(self) -> None:
        claimed = self._claim()
        operation_id = claimed.state.active_operation.operation_id
        exclusive = state.transition_operation(
            self.root,
            operation_id=operation_id,
            generation=1,
            expected_phase="reserved",
            next_phase="exclusive",
            owner_capability=CAPABILITY_ONE,
        )
        self.assertEqual(exclusive.generation, 1)
        self.assertEqual(exclusive.active_operation.phase, state.LifecyclePhase.EXCLUSIVE)
        next_capability = "c" * 43
        transferred = state.transfer_operation_owner(
            self.root,
            operation_id=operation_id,
            generation=1,
            expected_phase="exclusive",
            owner_capability=CAPABILITY_ONE,
            next_owner_kind="updater",
            next_owner_capability=next_capability,
        )
        self.assertEqual(transferred.generation, 2)
        with self.assertRaises(state.LifecycleConflictError):
            state.transition_operation(
                self.root,
                operation_id=operation_id,
                generation=1,
                expected_phase="exclusive",
                next_phase="verifying",
                owner_capability=CAPABILITY_ONE,
            )
        with self.assertRaises(state.OwnershipError):
            state.transition_operation(
                self.root,
                operation_id=operation_id,
                generation=2,
                expected_phase="exclusive",
                next_phase="verifying",
                owner_capability=CAPABILITY_ONE,
            )

    def test_corrupt_truncated_and_unsupported_state_fail_closed(self) -> None:
        invalid_cases = (
            (None, '{"schema_version":'),
            (None, '{"schema_version":1,"schema_version":1,"generation":0,"active_operation":null}'),
            ({"schema_version": 2, "generation": 0, "active_operation": None}, None),
            ({"schema_version": 1, "generation": "0", "active_operation": None}, None),
            ({"schema_version": 1, "generation": 0, "active_operation": None, "extra": True}, None),
            ({
                "schema_version": 1,
                "generation": 1,
                "active_operation": {
                    "operation_id": str(uuid.uuid4()),
                    "operation_type": "unsupported-operation",
                    "phase": "reserved",
                    "owner_kind": "updater",
                    "owner_nonce_hash": f"sha256:{'0' * 64}",
                },
            }, None),
            ({
                "schema_version": 1,
                "generation": 1,
                "active_operation": {
                    "operation_id": str(uuid.uuid4()),
                    "operation_type": "update",
                    "phase": "installing",
                    "owner_kind": "updater",
                    "owner_nonce_hash": f"sha256:{'0' * 64}",
                },
            }, None),
        )
        for payload, text in invalid_cases:
            with self.subTest(payload=payload, text=text):
                self._write_raw_state(payload, text=text)
                expected = (
                    state.UnsupportedSchemaError
                    if isinstance(payload, dict) and payload.get("schema_version") == 2
                    else state.CorruptStateError
                )
                with self.assertRaises(expected):
                    state.inspect_state(self.root)

    def test_atomic_replacement_permissions_and_capability_redaction(self) -> None:
        claimed = self._claim()
        paths = state.lifecycle_paths(self.root)
        first_inode = paths.state_path.stat().st_ino
        raw = paths.state_path.read_text(encoding="utf-8")
        self.assertNotIn(CAPABILITY_ONE, raw)
        self.assertIn(state.hash_owner_capability(CAPABILITY_ONE), raw)
        self.assertEqual(stat.S_IMODE(paths.state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.state_lock_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.lifecycle_dir.stat().st_mode), 0o700)
        state.transition_operation(
            self.root,
            operation_id=claimed.state.active_operation.operation_id,
            generation=1,
            expected_phase="reserved",
            next_phase="exclusive",
            owner_capability=CAPABILITY_ONE,
        )
        self.assertNotEqual(paths.state_path.stat().st_ino, first_inode)
        self.assertEqual(list(paths.lifecycle_dir.glob("*.tmp")), [])

    def test_directory_fsync_failure_is_reported_without_resetting_state(self) -> None:
        paths = state.lifecycle_paths(self.root)
        paths.run_dir.mkdir(mode=0o700)
        paths.lifecycle_dir.mkdir(mode=0o700)
        state.qualify_local_filesystem(
            paths.lifecycle_dir,
            require_private=True,
            description="lifecycle authority directory",
        )
        original = state._fsync_directory

        def fail_lifecycle_directory(directory: Path) -> None:
            if directory == paths.lifecycle_dir:
                raise OSError("simulated fsync failure")
            original(directory)

        with patch.object(state, "_fsync_directory", side_effect=fail_lifecycle_directory):
            with self.assertRaises(state.PersistenceError):
                self._claim()
        persisted = json.loads(paths.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["generation"], 1)
        self.assertIsNotNone(persisted["active_operation"])

    def test_indeterminate_claim_persistence_keeps_recovery_credential(self) -> None:
        paths = state.lifecycle_paths(self.root)
        paths.run_dir.mkdir(mode=0o700)
        paths.lifecycle_dir.mkdir(mode=0o700)
        paths.credentials_dir.mkdir(mode=0o700)
        state.qualify_local_filesystem(
            paths.lifecycle_dir,
            require_private=True,
            description="lifecycle authority directory",
        )
        operation_id = str(uuid.uuid4())
        original = state._fsync_directory

        def fail_state_directory(directory: Path) -> None:
            if directory == paths.lifecycle_dir:
                raise OSError("simulated state directory fsync failure")
            original(directory)

        with patch.object(state, "_fsync_directory", side_effect=fail_state_directory):
            with self.assertRaises(state.PersistenceError):
                state.claim_operation_with_credential(
                    self.root,
                    operation_type="update",
                    owner_kind="updater",
                    operation_id=operation_id,
                )
        self.assertEqual(state.inspect_state(self.root).active_operation.operation_id, operation_id)
        self.assertTrue(paths.credential_path(operation_id).is_file())

    def test_credential_file_is_private_and_removed_only_after_exact_completion(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        paths = state.lifecycle_paths(self.root)
        credential = paths.credential_path(operation.operation_id)
        self.assertTrue(credential.is_file())
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        with self.assertRaises(state.LifecycleConflictError):
            state.complete_operation_with_credential(
                self.root,
                operation_id=operation.operation_id,
                generation=2,
                expected_phase="reserved",
            )

    def test_typed_completion_cannot_release_a_foreign_operation(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type=state.OperationType.RESTORE,
            owner_kind=state.OwnerKind.RESTORE_CONTROLLER,
        )
        operation = claimed.state.active_operation
        assert operation is not None
        credential = state.lifecycle_paths(self.root).credential_path(operation.operation_id)
        with self.assertRaises(state.LifecycleConflictError):
            state.complete_operation_with_credential(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                expected_phase=state.LifecyclePhase.RESERVED,
                expected_operation_type=state.OperationType.UPDATE,
            )
        current = state.inspect_state(self.root)
        self.assertEqual(current.active_operation.operation_type, state.OperationType.RESTORE)
        self.assertTrue(credential.exists())
        state.complete_operation_with_credential(
            self.root,
            operation_id=operation.operation_id,
            generation=1,
            expected_phase="reserved",
        )
        self.assertFalse(credential.exists())

    def test_credential_write_retries_short_writes_until_payload_is_complete(self) -> None:
        self._prime_lifecycle_filesystem_qualification()
        original_write = os.write
        requested_sizes: list[int] = []

        def short_write(descriptor: int, payload: bytes | memoryview) -> int:
            requested_sizes.append(len(payload))
            limit = 5 if len(requested_sizes) == 1 else 3
            return original_write(descriptor, payload[:limit])

        with patch.object(state.os, "write", side_effect=short_write):
            claimed = state.claim_operation_with_credential(
                self.root,
                operation_type="update",
                owner_kind="updater",
            )
        operation = claimed.state.active_operation
        credential = state.lifecycle_paths(self.root).credential_path(operation.operation_id)
        capability = credential.read_text(encoding="ascii").strip()
        self.assertGreater(len(requested_sizes), 2)
        self.assertEqual(state.hash_owner_capability(capability), operation.owner_nonce_hash)

    def test_credential_zero_write_or_exception_fails_before_authority_claim(self) -> None:
        self._prime_lifecycle_filesystem_qualification()
        for failure in ("zero", "exception"):
            with self.subTest(failure=failure):
                operation_id = str(uuid.uuid4())
                write_patch = (
                    patch.object(state.os, "write", return_value=0)
                    if failure == "zero"
                    else patch.object(
                        state.os,
                        "write",
                        side_effect=OSError("simulated write failure"),
                    )
                )
                with write_patch:
                    with self.assertRaises(state.PersistenceError):
                        state.claim_operation_with_credential(
                            self.root,
                            operation_type="update",
                            owner_kind="updater",
                            operation_id=operation_id,
                        )
                self.assertIsNone(state.inspect_state(self.root).active_operation)
                self.assertFalse(
                    state.lifecycle_paths(self.root).credential_path(operation_id).exists()
                )

    def test_authoritative_completion_failure_keeps_operation_active(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        with patch.object(
            state,
            "_write_state_unlocked",
            side_effect=state.PersistenceError("simulated completion failure"),
        ):
            with self.assertRaises(state.PersistenceError):
                state.complete_operation_with_credential(
                    self.root,
                    operation_id=operation.operation_id,
                    generation=claimed.state.generation,
                    expected_phase="reserved",
                )
        self.assertEqual(
            state.inspect_state(self.root).active_operation.operation_id,
            operation.operation_id,
        )

    def test_credential_unlink_failure_is_cleanup_warning_after_completion(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        with patch.object(Path, "unlink", side_effect=PermissionError("simulated unlink failure")):
            result = state.complete_operation_with_credential(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                expected_phase="reserved",
            )
        self.assertIsNone(result.state.active_operation)
        self.assertEqual(result.credential_cleanup_error, "persistence_error")
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_credential_directory_fsync_failure_is_cleanup_warning(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        paths = state.lifecycle_paths(self.root)
        original_fsync = state._fsync_directory

        def fail_credential_directory(directory: Path) -> None:
            if directory == paths.credentials_dir:
                raise OSError("simulated credential directory fsync failure")
            original_fsync(directory)

        with patch.object(state, "_fsync_directory", side_effect=fail_credential_directory):
            result = state.complete_operation_with_credential(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                expected_phase="reserved",
            )
        self.assertEqual(result.credential_cleanup_error, "persistence_error")
        self.assertIsNone(state.inspect_state(self.root).active_operation)

    def test_cleanup_warning_retry_cannot_complete_a_new_operation(self) -> None:
        first = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        first_operation = first.state.active_operation
        with patch.object(Path, "unlink", side_effect=PermissionError("simulated unlink failure")):
            result = state.complete_operation_with_credential(
                self.root,
                operation_id=first_operation.operation_id,
                generation=first.state.generation,
                expected_phase="reserved",
            )
        self.assertIsNotNone(result.credential_cleanup_error)
        second = state.claim_operation_with_credential(
            self.root,
            operation_type="update",
            owner_kind="updater",
        )
        with self.assertRaises(state.LifecycleConflictError):
            state.complete_operation_with_credential(
                self.root,
                operation_id=first_operation.operation_id,
                generation=first.state.generation,
                expected_phase="reserved",
            )
        current = state.inspect_state(self.root)
        self.assertEqual(
            current.active_operation.operation_id,
            second.state.active_operation.operation_id,
        )

    def test_controller_lock_excludes_duplicates_and_lease_checks_start_identity(self) -> None:
        claimed = self._claim()
        operation = claimed.state.active_operation
        with state.acquire_controller_lease(
            self.root,
            operation_id=operation.operation_id,
            generation=1,
            expected_phase="reserved",
            owner_capability=CAPABILITY_ONE,
        ) as handle:
            lease = state.read_controller_lease(self.root)
            self.assertEqual(lease, handle.lease)
            self.assertTrue(state.controller_lease_is_current(lease))
            with self.assertRaises(state.ControllerLockError):
                state.acquire_controller_lease(
                    self.root,
                    operation_id=operation.operation_id,
                    generation=1,
                    expected_phase="reserved",
                    owner_capability=CAPABILITY_ONE,
                )
            with patch.object(state, "_process_start_identity", return_value="999999"):
                self.assertFalse(state.controller_lease_is_current(lease))
        self.assertIsNone(state.read_controller_lease(self.root))

    def test_controller_lock_is_released_by_os_when_owner_process_dies(self) -> None:
        claimed = self._claim()
        operation = claimed.state.active_operation
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        stop = context.Event()
        process = context.Process(
            target=_hold_controller_lock,
            args=(str(self.root), operation.operation_id, CAPABILITY_ONE, ready, stop),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(timeout=5))
            with self.assertRaises(state.ControllerLockError):
                state.acquire_controller_lease(
                    self.root,
                    operation_id=operation.operation_id,
                    generation=1,
                    expected_phase="reserved",
                    owner_capability=CAPABILITY_ONE,
                )
        finally:
            process.terminate()
            process.join(timeout=5)
        self.assertIsNotNone(process.exitcode)
        with state.acquire_controller_lease(
            self.root,
            operation_id=operation.operation_id,
            generation=1,
            expected_phase="reserved",
            owner_capability=CAPABILITY_ONE,
        ) as recovered:
            self.assertEqual(recovered.lease.pid, os.getpid())

    def test_successive_controller_takeovers_have_exactly_one_current_owner(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        self.assertIsNotNone(operation)
        first_handle, first = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            owner_capability=claimed.owner_capability,
            next_owner_kind="restore_controller",
        )
        self.assertEqual(first.state.generation, claimed.state.generation + 1)
        self.assertNotEqual(first.owner_capability, claimed.owner_capability)
        with self.assertRaises(state.LifecycleConflictError):
            state.validate_operation_owner_current_phase(
                self.root,
                operation_id=operation.operation_id,
                generation=claimed.state.generation,
                owner_capability=claimed.owner_capability,
            )
        with self.assertRaises(state.ControllerLockError):
            state.acquire_transferred_controller_lease(
                self.root,
                operation_id=operation.operation_id,
                generation=first.state.generation,
                expected_phase="reserved",
                owner_capability=first.owner_capability,
                next_owner_kind="restore_controller",
            )
        first_handle.release()

        second_handle, second = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation.operation_id,
            generation=first.state.generation,
            expected_phase="reserved",
            owner_capability=first.owner_capability,
            next_owner_kind="restore_controller",
        )
        try:
            self.assertEqual(second.state.generation, first.state.generation + 1)
            self.assertNotEqual(second.owner_capability, first.owner_capability)
            with self.assertRaises(state.LifecycleConflictError):
                state.validate_operation_owner_current_phase(
                    self.root,
                    operation_id=operation.operation_id,
                    generation=first.state.generation,
                    owner_capability=first.owner_capability,
                )
        finally:
            second_handle.release()

    def test_controller_transfer_recovers_orphaned_pre_cas_credential(self) -> None:
        claimed = state.claim_operation_with_credential(
            self.root,
            operation_type="restore",
            owner_kind="restore_controller",
        )
        operation = claimed.state.active_operation
        self.assertIsNotNone(operation)
        paths = state.lifecycle_paths(self.root)
        orphan = paths.generation_credential_path(
            operation.operation_id,
            claimed.state.generation + 1,
        )
        state._write_credential_path(paths, orphan, "z" * 43)
        handle, transferred = state.acquire_transferred_controller_lease(
            self.root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            owner_capability=claimed.owner_capability,
            next_owner_kind="restore_controller",
        )
        try:
            self.assertEqual(transferred.state.generation, claimed.state.generation + 1)
            self.assertNotEqual(orphan.read_text(encoding="ascii").strip(), "z" * 43)
        finally:
            handle.release()


if __name__ == "__main__":
    unittest.main()
