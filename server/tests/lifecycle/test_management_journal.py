from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from lifecycle import management_journal as contract, state


def _execution_payload() -> dict:
    python_id, node_id, web_id = (str(uuid.uuid4()) for _ in range(3))
    runtime = "/opt/streamhome/runtime"
    return {
        "application_commit": "a" * 40, "application_build_id": "fixture-build.1",
        "deployment_revision": str(uuid.uuid4()), "runtime_bundle_id": str(uuid.uuid4()),
        "runtime_manifest_sha256": "b" * 64,
        "python_environment_id": python_id,
        "python_executable": f"{runtime}/python/{python_id}/venv/bin/python",
        "node_environment_id": node_id, "node_executable": f"{runtime}/node/{node_id}/bin/node",
        "web_bundle_id": web_id, "web_dependency_target": f"{runtime}/web/{web_id}/node_modules",
        "web_dist_target": f"{runtime}/web/{web_id}/dist", "web_asset_manifest_sha256": "c" * 64,
        "control_bundle_id": str(uuid.uuid4()), "control_manifest_sha256": "d" * 64,
        "control_protocol_version": 1,
        "policy_revisions": {"session": 3, "storage": 2}, "provider_revisions": {"drive": 1},
    }


def _journal_payload() -> dict:
    execution = _execution_payload()
    candidate = deepcopy(execution)
    candidate["deployment_revision"] = str(uuid.uuid4())
    return {
        "schema_version": 1, "installation_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
        "generation": 8, "action": "configure_apply", "activation_mode": "cutover",
        "review_id": str(uuid.uuid4()), "previous": execution, "candidate": candidate,
        "initial_service_state": "running", "controller_invocation_id": "e" * 32,
        "supervisor_invocation_id": "f" * 32, "stage": "reserved", "acceptance": None,
    }


class ManagementJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = _journal_payload()

    def _journal(self, **changes: object) -> contract.ManagementJournal:
        return contract.parse_management_journal({**self.payload, **changes})

    def _acceptance(self) -> dict[str, str]:
        candidate = contract.parse_execution_tuple(self.payload["candidate"])
        return {"execution_tuple_sha256": contract.execution_tuple_sha256(candidate), "verification_sha256": "1" * 64}

    def _authority(self, journal: contract.ManagementJournal, phase: str = "reserved") -> state.LifecycleState:
        return state.LifecycleState(2, journal.generation, state.ActiveOperation(
            journal.operation_id, state.OperationType.MANAGEMENT, state.LifecyclePhase(phase),
            state.OwnerKind.MANAGEMENT_CONTROLLER, state.hash_owner_capability("a" * 43),
        ))

    def test_round_trip_is_canonical_immutable_and_contains_no_owner_capability(self) -> None:
        journal = self._journal()
        raw = json.dumps(contract.management_journal_payload(journal))
        self.assertEqual(contract.parse_management_journal_json(raw), journal)
        self.assertEqual(contract.parse_management_journal_json(raw.encode()), journal)
        self.assertIsInstance(journal.candidate.policy_revisions, tuple)
        self.assertNotIn("owner_capability", raw)
        self.assertNotIn("owner_nonce_hash", raw)
        self.assertEqual(contract.management_journal_payload(journal), self.payload)

    def test_import_does_not_load_application_or_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            result = subprocess.run([
                sys.executable, "-c", "import sys; from lifecycle import management_journal; "
                "assert {'config', 'db', 'main', 'application', 'models', 'services', 'routes'}.isdisjoint(sys.modules)",
            ], cwd=directory, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_previous_target_is_explicit_version_two_and_accepts_only_previous_tuple(self):
        payload = {**self.payload, 'schema_version': 2, 'recovery_target': 'previous', 'stage': 'verifying'}
        journal = contract.parse_management_journal(payload)
        self.assertEqual(contract.management_journal_payload(journal), payload)
        self.assertEqual(contract.selected_execution(journal), journal.previous)
        acceptance = {'execution_tuple_sha256': contract.execution_tuple_sha256(journal.previous), 'verification_sha256': '1' * 64}
        accepted = contract.parse_management_journal({**payload, 'stage': 'accepted', 'acceptance': acceptance})
        terminal = contract.parse_management_journal({**payload, 'stage': 'rolled_back', 'acceptance': acceptance})
        contract.validate_journal_transition(accepted, terminal)
        for changed in ({'acceptance': self._acceptance()}, {'stage': 'succeeded'}, {'schema_version': 1},
                        {'recovery_target': 'candidate'}, {'activation_mode': 'save_only'}):
            with self.subTest(changed=changed), self.assertRaises(contract.ManagementJournalError):
                contract.parse_management_journal({**payload, 'stage': 'accepted', 'acceptance': acceptance, **changed})

    def test_ordinary_progress_cannot_turn_candidate_acceptance_into_previous_acceptance(self):
        candidate = self._journal(stage='accepted', acceptance=self._acceptance())
        previous = replace(candidate, recovery_target='previous',
            acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(candidate.previous), '1' * 64))
        with self.assertRaises(contract.ManagementJournalError):
            contract.validate_journal_transition(candidate, previous)

    def test_unknown_and_missing_fields_are_rejected_at_every_level(self) -> None:
        for key in self.payload:
            bad = deepcopy(self.payload)
            bad.pop(key)
            with self.subTest(missing=key), self.assertRaises(contract.ManagementJournalError):
                contract.parse_management_journal(bad)
        for location in ((), ("previous",), ("candidate",)):
            bad = deepcopy(self.payload)
            target = bad if not location else bad[location[0]]
            target["unknown"] = True
            with self.subTest(location=location), self.assertRaises(contract.ManagementJournalError):
                contract.parse_management_journal(bad)
        bad = self._acceptance()
        bad["owner_capability"] = "a" * 43
        with self.assertRaises(contract.ManagementJournalError):
            self._journal(stage="accepted", acceptance=bad)

    def test_duplicate_nonfinite_truncated_and_oversized_json_fail_closed(self) -> None:
        valid = json.dumps(self.payload)
        cases = (
            valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
            valid.replace('"generation": 8', '"generation": NaN'),
            valid.replace('"session": 3', '"session": 3, "session": 4'),
            valid[:-1], b"\xff", "[" * 1500 + "]" * 1500,
            valid + " " * contract.MAX_JOURNAL_BYTES,
        )
        for raw in cases:
            with self.subTest(raw=repr(raw)[:100]), self.assertRaises(contract.ManagementJournalError):
                contract.parse_management_journal_json(raw)

    def test_versions_generations_and_uuid_representation_are_strict(self) -> None:
        for key, values in {
            "schema_version": (True, 2, "1", 1.0), "generation": (True, 0, -1, "8", 1 << 63),
            "operation_id": (None, "../operation", "00000000-0000-0000-0000-000000000000"),
            "installation_id": (str(uuid.uuid4()).upper(),), "review_id": (uuid.uuid4().hex,),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(contract.ManagementJournalError):
                    self._journal(**{key: value})

    def test_only_reviewed_action_mode_pairs_are_valid(self) -> None:
        for action in contract.ManagementAction:
            for mode in contract.ActivationMode:
                candidate = deepcopy(self.payload["previous"])
                allowed = mode is contract.ActivationMode.CUTOVER or (
                    action is contract.ManagementAction.CONFIGURE_APPLY and mode is contract.ActivationMode.SAVE_ONLY
                    or action is contract.ManagementAction.PROVISION_CONTROL and mode is contract.ActivationMode.OS_ONLY
                )
                with self.subTest(action=action, mode=mode):
                    if allowed:
                        self._journal(action=action.value, activation_mode=mode.value, candidate=candidate)
                    else:
                        with self.assertRaises(contract.ManagementJournalError):
                            self._journal(action=action.value, activation_mode=mode.value, candidate=candidate)
        for key in ("action", "activation_mode", "stage"):
            with self.subTest(key=key), self.assertRaises(contract.ManagementJournalError):
                self._journal(**{key: "force"})

    def test_execution_paths_are_exact_and_web_targets_share_the_bound_bundle(self) -> None:
        for name in ("python_executable", "node_executable", "web_dependency_target", "web_dist_target"):
            for target in ("/tmp/tool", "../tool", self.payload["candidate"][name] + "/.."):
                bad = deepcopy(self.payload["candidate"])
                bad[name] = target
                with self.subTest(name=name, target=target), self.assertRaises(contract.ManagementJournalError):
                    contract.parse_execution_tuple(bad)
        bad = deepcopy(self.payload["candidate"])
        bad["web_dist_target"] = f"/opt/streamhome/runtime/web/{uuid.uuid4()}/dist"
        with self.assertRaises(contract.ManagementJournalError):
            contract.parse_execution_tuple(bad)

    def test_unbound_source_artifact_and_domain_identities_are_rejected(self) -> None:
        for key, value in (
            ("application_commit", "a" * 12), ("application_build_id", "../build"),
            ("runtime_manifest_sha256", "A" * 64), ("web_asset_manifest_sha256", "c" * 63),
            ("control_manifest_sha256", "sha256:" + "d" * 64), ("control_protocol_version", True),
            ("control_protocol_version", 2), ("policy_revisions", {"storage": True}),
            ("provider_revisions", {"drive": -1}), ("policy_revisions", {"../domain": 1}),
        ):
            bad = deepcopy(self.payload["candidate"])
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(contract.ManagementJournalError):
                contract.parse_execution_tuple(bad)

    def test_hash_binds_every_tuple_field_and_normalizes_only_object_order(self) -> None:
        execution = contract.parse_execution_tuple(self.payload["candidate"])
        digest = contract.execution_tuple_sha256(execution)
        reordered = dict(reversed(list(self.payload["candidate"].items())))
        reordered["policy_revisions"] = {"storage": 2, "session": 3}
        self.assertEqual(contract.execution_tuple_sha256(contract.parse_execution_tuple(reordered)), digest)
        for modified in (
            replace(execution, application_commit="b" * 40),
            replace(execution, deployment_revision=str(uuid.uuid4())),
            replace(execution, runtime_manifest_sha256="c" * 64),
            replace(execution, policy_revisions=(("session", 4), ("storage", 2))),
        ):
            self.assertNotEqual(contract.execution_tuple_sha256(modified), digest)
        with self.assertRaises(contract.ManagementJournalError):
            contract.execution_tuple_sha256(replace(execution, policy_revisions=(("session", 1), ("session", 3))))

    def test_stopped_service_has_no_fictional_supervisor_invocation(self) -> None:
        self._journal(initial_service_state="stopped", supervisor_invocation_id=None)
        for changes in ({"supervisor_invocation_id": None}, {"initial_service_state": "stopped"},
                        {"controller_invocation_id": "not-an-invocation"},
                        {"controller_invocation_id": "0" * 32}, {"supervisor_invocation_id": "0" * 32}):
            with self.subTest(changes=changes), self.assertRaises(contract.ManagementJournalError):
                self._journal(**changes)

    def test_actions_cannot_change_source_policies_or_other_domains(self) -> None:
        for action in contract.ManagementAction:
            for key, value in (
                ("application_commit", "b" * 40), ("application_build_id", "another-build"),
                ("policy_revisions", {"session": 4}), ("provider_revisions", {"drive": 2}),
            ):
                candidate = deepcopy(self.payload["previous"])
                candidate[key] = value
                with self.subTest(action=action, key=key), self.assertRaises(contract.ManagementJournalError):
                    self._journal(action=action.value, candidate=candidate)

    def test_environment_repair_preserves_deployment_control_and_assets(self) -> None:
        candidate = deepcopy(self.payload["previous"])
        candidate["runtime_bundle_id"] = str(uuid.uuid4())
        candidate["runtime_manifest_sha256"] = "f" * 64
        self._journal(action="repair_environment", candidate=candidate)
        for key, value in (("deployment_revision", str(uuid.uuid4())), ("control_bundle_id", str(uuid.uuid4())),
                           ("web_asset_manifest_sha256", "f" * 64)):
            bad = deepcopy(candidate)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(contract.ManagementJournalError):
                self._journal(action="repair_environment", candidate=bad)

    def test_control_provisioning_changes_only_control_and_os_only_changes_nothing(self) -> None:
        candidate = deepcopy(self.payload["previous"])
        candidate["control_bundle_id"] = str(uuid.uuid4())
        candidate["control_manifest_sha256"] = "f" * 64
        self._journal(action="provision_control", candidate=candidate)
        with self.assertRaises(contract.ManagementJournalError):
            self._journal(action="provision_control", activation_mode="os_only", candidate=candidate)
        self._journal(action="provision_control", activation_mode="os_only", candidate=self.payload["previous"])

    def test_save_only_cannot_launch_verify_or_accept_a_runtime(self) -> None:
        self._journal(activation_mode="save_only", stage="saved")
        for stage in ("exclusive", "verifying", "accepted", "succeeded", "rolled_back", "os_completed"):
            with self.subTest(stage=stage), self.assertRaises(contract.ManagementJournalError):
                self._journal(activation_mode="save_only", stage=stage)
        with self.assertRaises(contract.ManagementJournalError):
            self._journal(activation_mode="save_only", stage="saved", acceptance=self._acceptance())

    def test_acceptance_requires_exact_candidate_and_can_never_authorize_rollback(self) -> None:
        acceptance = self._acceptance()
        self._journal(stage="accepted", acceptance=acceptance)
        self._journal(stage="succeeded", acceptance=acceptance)
        self._journal(stage="recovery_required", acceptance=acceptance)
        self._journal(stage="recovery_required", acceptance=None)
        for stage in ("accepted", "succeeded"):
            with self.subTest(stage=stage), self.assertRaises(contract.ManagementJournalError):
                self._journal(stage=stage)
        for stage in ("reserved", "prepared", "exclusive", "verifying", "rolled_back"):
            with self.subTest(stage=stage), self.assertRaises(contract.ManagementJournalError):
                self._journal(stage=stage, acceptance=acceptance)
        with self.assertRaises(contract.ManagementJournalError):
            self._journal(stage="accepted", acceptance={**acceptance, "execution_tuple_sha256": "0" * 64})

    def test_progress_requires_every_stage_and_immutable_reviewed_inputs(self) -> None:
        previous = self._journal()
        contract.validate_journal_transition(previous, previous)
        for stage in ("prepared", "exclusive", "verifying", "accepted", "succeeded"):
            acceptance = self._acceptance() if stage in ("accepted", "succeeded") else None
            updated = self._journal(stage=stage, acceptance=acceptance)
            contract.validate_journal_transition(previous, updated)
            previous = updated
        with self.assertRaises(contract.ManagementJournalError):
            contract.validate_journal_transition(self._journal(), self._journal(stage="verifying"))
        for changes in ({"review_id": str(uuid.uuid4())}, {"generation": 9}, {"controller_invocation_id": "1" * 32}):
            with self.subTest(changes=changes), self.assertRaises(contract.ManagementJournalError):
                contract.validate_journal_transition(self._journal(), self._journal(stage="prepared", **changes))

    def test_accepted_failure_retains_acceptance_and_recovery_cannot_implicitly_resume(self) -> None:
        acceptance = self._acceptance()
        accepted = self._journal(stage="accepted", acceptance=acceptance)
        failed = self._journal(stage="recovery_required", acceptance=acceptance)
        contract.validate_journal_transition(accepted, failed)
        with self.assertRaises(contract.ManagementJournalError):
            contract.validate_journal_transition(accepted, self._journal(stage="recovery_required"))
        with self.assertRaises(contract.ManagementJournalError):
            contract.validate_journal_transition(failed, self._journal(stage="exclusive"))

    def test_active_comparison_requires_exact_schema_type_owner_operation_and_generation(self) -> None:
        journal = self._journal()
        authority = self._authority(journal)
        contract.validate_active_journal(journal, authority)
        operation = authority.active_operation
        for bad in (
            replace(authority, schema_version=1), replace(authority, generation=9),
            replace(authority, active_operation=None),
            replace(authority, active_operation=replace(operation, operation_id=str(uuid.uuid4()))),
            replace(authority, active_operation=replace(operation, operation_type=state.OperationType.UPDATE)),
            replace(authority, active_operation=replace(operation, owner_kind=state.OwnerKind.UPDATER)),
        ):
            with self.subTest(authority=bad), self.assertRaises(contract.ManagementJournalError):
                contract.validate_active_journal(journal, bad)

    def test_stage_never_waives_canonical_phase_validation(self) -> None:
        for stage, phase in (("reserved", "reserved"), ("prepared", "reserved"), ("exclusive", "exclusive"),
                             ("verifying", "verifying"), ("accepted", "verifying"), ("recovery_required", "recovery_required")):
            acceptance = self._acceptance() if stage == "accepted" else None
            journal = self._journal(stage=stage, acceptance=acceptance)
            contract.validate_active_journal(journal, self._authority(journal, phase))
            wrong = "exclusive" if phase != "exclusive" else "reserved"
            with self.subTest(stage=stage), self.assertRaises(contract.ManagementJournalError):
                contract.validate_active_journal(journal, self._authority(journal, wrong))


if __name__ == "__main__":
    unittest.main()
