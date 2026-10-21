from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from lifecycle import bootstrap, recovery, state


def _competing_claim(root: str, operation_type: str, owner: str, queue: object) -> None:
    try:
        claimed = state.claim_operation(root, operation_type=operation_type, owner_kind=owner)
        queue.put(("claimed", claimed.state.schema_version, claimed.state.generation))
    except state.LifecycleConflictError:
        queue.put(("conflict",))


class ManagementAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "server").mkdir()
        for name in ("start.sh", "update.sh"):
            (self.root / name).write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture_authority(self, version: int = 2, generation: int = 7) -> None:
        # Direct disposable fixture publication is not a production migration API.
        paths = state.lifecycle_paths(self.root)
        state._ensure_lifecycle_directories(paths)
        state._atomic_write_json(paths.state_path, {
            "schema_version": version, "generation": generation, "active_operation": None,
        })

    def _claim(self, operation_type: state.OperationType = state.OperationType.MANAGEMENT) -> state.ClaimResult:
        return state.claim_operation_with_credential(
            self.root, operation_type=operation_type, owner_kind=state.OPERATION_OWNERS[operation_type],
        )

    def _state_bytes(self) -> bytes:
        return state.lifecycle_paths(self.root).state_path.read_bytes()

    def test_missing_authority_remains_read_only_version_one(self) -> None:
        self.assertEqual(state.inspect_state(self.root), state.LifecycleState(1, 0, None))
        self.assertFalse((self.root / ".run").exists())

    def test_legacy_claim_does_not_upgrade(self) -> None:
        claimed = self._claim(state.OperationType.UPDATE)
        self.assertEqual(claimed.state.schema_version, 1)
        self.assertEqual(state.inspect_state(self.root).schema_version, 1)

    def test_management_claim_never_migrates_legacy_authority(self) -> None:
        self._fixture_authority(1)
        before = self._state_bytes()
        with self.assertRaises(state.UnsupportedSchemaError):
            self._claim()
        self.assertEqual(self._state_bytes(), before)
        self.assertEqual(list(state.lifecycle_paths(self.root).credentials_dir.iterdir()), [])

    def test_version_one_rejects_both_management_enum_values(self) -> None:
        self._fixture_authority(1)
        for operation_type, owner in (("management", "updater"), ("update", "management_controller")):
            with self.subTest(operation_type=operation_type, owner=owner):
                payload = {
                    "schema_version": 1, "generation": 1,
                    "active_operation": {
                        "operation_id": str(uuid.uuid4()), "operation_type": operation_type,
                        "owner_kind": owner, "phase": "reserved",
                        "owner_nonce_hash": state.hash_owner_capability("a" * 43),
                    },
                }
                with self.assertRaises(state.UnsupportedSchemaError):
                    state._parse_state_payload(payload)

    def test_version_two_requires_exact_operation_owner_pairs(self) -> None:
        self._fixture_authority()
        before = self._state_bytes()
        for operation_type in state.OperationType:
            for owner in state.OwnerKind:
                if state.OPERATION_OWNERS[operation_type] is owner:
                    continue
                with self.subTest(operation_type=operation_type, owner=owner):
                    with self.assertRaises(ValueError):
                        state.claim_operation(self.root, operation_type=operation_type, owner_kind=owner)
                    self.assertEqual(self._state_bytes(), before)

    def test_version_two_round_trips_all_operations_without_downgrade(self) -> None:
        self._fixture_authority()
        for operation_type in state.OperationType:
            with self.subTest(operation_type=operation_type):
                claimed = self._claim(operation_type)
                operation_id = claimed.state.active_operation.operation_id
                for expected, target in (("reserved", "exclusive"), ("exclusive", "verifying")):
                    current = state.transition_operation_with_credential(
                        self.root, operation_id=operation_id, generation=claimed.state.generation,
                        expected_phase=expected, next_phase=target,
                    )
                    self.assertEqual(current.schema_version, 2)
                completed = state.complete_operation_with_credential(
                    self.root, operation_id=operation_id, generation=claimed.state.generation,
                    expected_phase="verifying", expected_operation_type=operation_type,
                )
                self.assertEqual(completed.state, state.LifecycleState(2, claimed.state.generation, None))
                self.assertEqual(state.inspect_state(self.root), completed.state)

    def test_management_excludes_every_claim_including_recovery(self) -> None:
        self._fixture_authority()
        claimed = self._claim()
        operation_id = claimed.state.active_operation.operation_id
        for phase in ("reserved", "recovery_required"):
            if phase == "recovery_required":
                state.transition_operation_with_credential(
                    self.root, operation_id=operation_id, generation=claimed.state.generation,
                    expected_phase="reserved", next_phase=phase,
                )
            before = self._state_bytes()
            for operation_type in state.OperationType:
                with self.subTest(phase=phase, operation_type=operation_type):
                    with self.assertRaises(state.LifecycleConflictError):
                        self._claim(operation_type)
                    self.assertEqual(self._state_bytes(), before)

    def test_competing_processes_share_one_version_two_authority(self) -> None:
        self._fixture_authority()
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(
            target=_competing_claim,
            args=(str(self.root), operation_type.value, owner.value, queue),
        ) for operation_type, owner in state.OPERATION_OWNERS.items()]
        try:
            for process in processes:
                process.start()
            results = [queue.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(results.count(("claimed", 2, 8)), 1)
            self.assertEqual(results.count(("conflict",)), 2)
            self.assertEqual(state.inspect_state(self.root).generation, 8)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            queue.close()
            queue.join_thread()

    def test_controller_handoff_preserves_version_and_fences_old_capability(self) -> None:
        self._fixture_authority()
        claimed = self._claim()
        operation_id = claimed.state.active_operation.operation_id
        handle, transferred = state.acquire_transferred_controller_lease(
            self.root, operation_id=operation_id, generation=claimed.state.generation,
            expected_phase="reserved", owner_capability=claimed.owner_capability,
            next_owner_kind="management_controller",
        )
        try:
            self.assertEqual(transferred.state.schema_version, 2)
            self.assertEqual(transferred.state.generation, 9)
            self.assertNotEqual(transferred.owner_capability, claimed.owner_capability)
            self.assertEqual(state.read_controller_lease(self.root), handle.lease)
            self.assertEqual(handle.lease.schema_version, 2)
            self.assertTrue(state.controller_lease_is_current(handle.lease))
            for generation, capability in ((8, claimed.owner_capability), (9, claimed.owner_capability)):
                with self.subTest(generation=generation):
                    with self.assertRaises(state.LifecycleError):
                        state.complete_operation(
                            self.root, operation_id=operation_id, generation=generation,
                            expected_phase="reserved", owner_capability=capability,
                        )
            current = state.validate_operation_with_credential(
                self.root, operation_id=operation_id, generation=9, expected_phase="reserved",
            )
            self.assertEqual(current, transferred.state)
        finally:
            handle.release()
        self.assertIsNone(state.read_controller_lease(self.root))

    def test_owner_transfer_cannot_relabel_management_as_update(self) -> None:
        self._fixture_authority()
        claimed = self._claim()
        before = self._state_bytes()
        with self.assertRaises(ValueError):
            state.transfer_operation_owner(
                self.root, operation_id=claimed.state.active_operation.operation_id,
                generation=claimed.state.generation, expected_phase="reserved",
                owner_capability=claimed.owner_capability,
                next_owner_kind="updater", next_owner_capability="a" * 43,
            )
        self.assertEqual(self._state_bytes(), before)

    def test_lease_version_one_rejects_management_and_future_versions(self) -> None:
        for version in (1, 3, True):
            with self.subTest(version=version):
                with self.assertRaises(state.UnsupportedSchemaError):
                    state._parse_lease_payload({
                        "schema_version": version, "operation_id": str(uuid.uuid4()),
                        "generation": 8, "owner_kind": "management_controller",
                        "pid": os.getpid(), "process_start_identity": "123",
                    })

    def test_management_startup_rejects_legacy_and_invented_permits(self) -> None:
        self._fixture_authority()
        claimed = self._claim()
        for mode in ("", "update_verify", "restore_verify", "management_verify", "recovery_only"):
            with self.subTest(mode=mode):
                context = bootstrap.evaluate_startup(self.root, {
                    bootstrap.STARTUP_MODE_ENV: mode,
                    bootstrap.OPERATION_ID_ENV: claimed.state.active_operation.operation_id,
                    bootstrap.GENERATION_ENV: str(claimed.state.generation),
                }, install=False)
                self.assertEqual(context.mode, bootstrap.StartupMode.FAIL_CLOSED)
                self.assertEqual(context.failure_code, "management_guarded_permit_mismatch" if mode == "management_verify" else "management_startup_unavailable")
                self.assertFalse(context.guarded_identity_validated)
                self.assertEqual(context.allowed_effects, frozenset())

    def test_legacy_recovery_cannot_dispatch_management_as_update(self) -> None:
        self._fixture_authority()
        claimed = self._claim()
        operation_id = claimed.state.active_operation.operation_id
        state.transition_operation_with_credential(
            self.root, operation_id=operation_id, generation=8,
            expected_phase="reserved", next_phase="recovery_required",
        )
        before = self._state_bytes()
        with patch.object(recovery, "_durable_recovery_evidence") as evidence:
            with self.assertRaises(recovery.RecoveryPlanningConflict):
                recovery.plan_recovery(self.root, operation_id=operation_id, generation=8)
            evidence.assert_not_called()
        self.assertFalse(recovery.normalize_interrupted_operator(self.root, operation_id, 8))
        self.assertEqual(self._state_bytes(), before)

    def test_legacy_cli_cannot_claim_transition_or_complete_management(self) -> None:
        self._fixture_authority()
        claimed = self._claim()
        operation_id = claimed.state.active_operation.operation_id
        before = self._state_bytes()
        for arguments in (
            ["claim", "--operation-type", "management", "--owner-kind", "management_controller"],
            ["transition", "--operation-id", operation_id, "--generation", "8",
             "--expected-phase", "reserved", "--next-phase", "exclusive"],
            ["complete", "--operation-id", operation_id, "--generation", "8", "--expected-phase", "reserved"],
        ):
            with self.subTest(command=arguments[0]):
                result = subprocess.run(
                    [sys.executable, "-m", "lifecycle.cli", "--root", str(self.root), *arguments],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(json.loads(result.stderr)["ok"])
                self.assertNotIn(claimed.owner_capability, result.stdout + result.stderr)
                self.assertEqual(self._state_bytes(), before)


if __name__ == "__main__":
    unittest.main()
