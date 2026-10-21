"""Separate-unit MANAGEMENT execution with durable acceptance before recovery."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import urllib.error
import urllib.request

from . import management_journal as contract, management_store, state
from .configuration_store import ConfigurationStore, DeploymentRevision
from .control.processes import controller_peer, process_cgroup, RUNTIME_CGROUP
from .control.requests import ManagementRequest, read_management_request, read_os_receipt
from .control.rpc import ControlError, request
from .control.tickets import read_stop_epoch
from .execution_state import read_active_execution, publish_accepted_execution
from .layout import layout_for_root
from .runtime_build import verify_runtime
from .runtime_links import bind_management_runtime


ROOT = Path("/opt/streamhome")


def stop_epoch() -> str:
    return read_stop_epoch()


class ManagementController:
    def __init__(self, reviewed: ManagementRequest, *, control_manifest_sha256: str) -> None:
        self.review = reviewed
        self.layout = layout_for_root(ROOT)
        selected = self.layout.descriptor.descriptor
        if (not self.layout.managed or os.geteuid() != selected.service_uid or os.geteuid() == 0
                or selected.installation_id != reviewed.installation_id
                or control_manifest_sha256 != reviewed.controller_control_manifest_sha256):
            raise ControlError("control_controller_forbidden")
        controller_peer(os.getpid(), reviewed.operation_id)
        self.invocation = contract._text(os.environ.get("INVOCATION_ID", ""), contract._INVOCATION, "controller invocation")
        if self.invocation == "0" * 32:
            raise ControlError("control_controller_forbidden")
        self.uid = selected.service_uid
        self.claim = None
        self.lease = None
        self.journal = None
        self.cancelled = False
        self.preparing = False
        self.descriptor = os.open(self.layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.store = ConfigurationStore(self.descriptor, reviewed.installation_id)
        os.close(self.descriptor)

    def checkpoint(self) -> None:
        if self.cancelled or self.review.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip() or self.review.stop_epoch != stop_epoch():
            raise ControlError("control_operator_stopped")
        if self.lease is not None:
            self.lease.require_current()
        supervisor = self.review.supervisor
        if supervisor is not None:
            if state._process_start_identity(supervisor.pid) != supervisor.start_identity or process_cgroup(supervisor.pid) != RUNTIME_CGROUP:
                raise ControlError("control_incarnation_changed")

    def rpc(self, service: str, action: str, *, timeout: float = 60) -> dict:
        self.checkpoint()
        reference = self.review.supervisor if service == "supervisor" else self.review.backend
        if reference is None or self.journal is None:
            raise ControlError("control_runtime_unavailable")
        return request(self.layout.runtime / f"{service}.sock", expected_uid=self.uid,
                       expected_pid=reference.pid, installation_id=self.review.installation_id,
                       invocation=self.review.supervisor_invocation_id, action=action,
                       arguments={"operation_id": self.journal.operation_id, "generation": self.journal.generation}, timeout=timeout)

    def advance(self, stage: contract.ManagementStage, *, acceptance=None) -> None:
        self.checkpoint()
        updated = replace(self.journal, stage=stage, acceptance=self.journal.acceptance if acceptance is None else acceptance)
        self.journal = management_store.advance_management_journal(ROOT, self.journal, updated, lease=self.lease)

    def phase(self, before: state.LifecyclePhase, after: state.LifecyclePhase) -> None:
        self.checkpoint()
        state.transition_operation_with_credential(ROOT, operation_id=self.journal.operation_id,
            generation=self.journal.generation, expected_phase=before, next_phase=after)

    def wait_os_receipt(self) -> None:
        deadline = time.monotonic() + 90
        while True:
            self.checkpoint()
            try:
                read_os_receipt(self.review, self.journal.generation, self.invocation)
                return
            except ControlError as exc:
                if exc.code != "control_ticket_unavailable":
                    raise
            if time.monotonic() >= deadline:
                raise ControlError("control_os_bridge_timeout")
            time.sleep(0.2)

    def http(self, path: str, *, body: dict | None = None, capability: str | None = None) -> tuple[dict, dict]:
        headers = {"Accept": "application/json"}
        if capability is not None:
            headers["X-StreamHome-Management-Commit"] = capability
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body, allow_nan=False).encode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request_object = urllib.request.Request("http://127.0.0.1:8000" + path, data=raw, headers=headers)
        with opener.open(request_object, timeout=2) as response:
            raw = response.read(32769)
            if len(raw) > 32768:
                raise ControlError("control_health_invalid")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ControlError("control_health_invalid")
            return value, dict(response.headers)

    def wait_guarded(self) -> dict:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            self.checkpoint()
            try:
                health, _ = self.http("/api/health")
                live, _ = self.http("/api/health/live")
                if (health.get("readyKind") == "guarded" and health.get("buildId") == self.journal.candidate.application_build_id
                        and live.get("mode") == "management_verify" and live.get("workersStarted") is False
                        and live.get("generalReady") is False and live.get("recoverySucceeded") is True):
                    # Check the actual web listener too. The proxy must serve the
                    # same build and guarded backend, on the reviewed port.
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(f"http://127.0.0.1:{self.review.deployment.web_port}/api/health", timeout=2) as response:
                        web = json.loads(response.read(32769))
                        if response.headers.get("X-StreamHome-Web-Build") != self.journal.candidate.application_build_id or web.get("readyKind") != "guarded" or web.get("buildId") != self.journal.candidate.application_build_id:
                            raise ControlError("control_web_identity_changed")
                    return {"backend": live, "web_build_id": self.journal.candidate.application_build_id,
                            "execution_sha256": contract.execution_tuple_sha256(self.journal.candidate),
                            "supervisor_invocation_id": self.review.supervisor_invocation_id}
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.25)
        raise ControlError("control_guarded_start_timeout")

    def commit_and_wait_recovery(self) -> None:
        payload = {"operation_id": self.journal.operation_id, "generation": self.journal.generation,
                   "build_id": self.journal.candidate.application_build_id,
                   "execution_tuple_sha256": contract.execution_tuple_sha256(self.journal.candidate)}
        deadline = time.monotonic() + 120
        acknowledged = False
        while time.monotonic() < deadline:
            self.checkpoint()
            try:
                if not acknowledged:
                    result, _ = self.http("/api/lifecycle/management/commit", body=payload, capability=self.claim.owner_capability)
                    acknowledged = result.get("committed") is True and result.get("buildId") == payload["build_id"]
                if acknowledged:
                    live, _ = self.http("/api/health/live")
                    if (live.get("buildId") == payload["build_id"] and live.get("recoverySucceeded") is True
                            and live.get("workersStarted") is True and live.get("errorCode") in (None, "runtime_not_settled")):
                        return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.25)
        raise ControlError("control_accepted_recovery_required")

    def complete(self) -> None:
        self.checkpoint()
        phase = contract.journal_phase(self.journal)
        state.complete_operation_with_credential(ROOT, operation_id=self.journal.operation_id,
            generation=self.journal.generation, expected_phase=phase)

    def execute(self) -> None:
        self.checkpoint()
        authority = state.inspect_state(ROOT)
        manifest = self.store.inspect()
        if (authority.schema_version != 2 or authority.active_operation is not None or authority.generation != self.review.expected_generation
                or read_active_execution(ROOT).execution != self.review.previous or manifest.desired != self.review.expected_desired
                or manifest.active.revision != self.review.previous.deployment_revision
                or manifest.policy_revisions != self.review.previous.policy_revisions or manifest.provider_revisions != self.review.previous.provider_revisions):
            raise ControlError("control_review_stale")
        if self.review.action is not contract.ManagementAction.CONFIGURE_APPLY and self.store.read_revision(manifest.active).settings != self.review.deployment:
            raise ControlError("control_review_stale")
        if self.review.activation_mode is contract.ActivationMode.CUTOVER:
            verify_runtime(ROOT, self.review.candidate)
        self.lease, self.claim = state.claim_management_controller(ROOT, operation_id=self.review.operation_id, expected_generation=self.review.expected_generation)
        journal = contract.ManagementJournal(
            self.review.installation_id, self.review.operation_id, self.claim.state.generation,
            self.review.action, self.review.activation_mode, self.review.review_id,
            self.review.previous, self.review.candidate, self.review.initial_service_state,
            self.invocation, self.review.supervisor_invocation_id, contract.ManagementStage.RESERVED, None,
        )
        self.journal = management_store.create_management_journal(ROOT, journal, lease=self.lease)
        if self.review.backend is not None:
            if self.review.activation_mode is contract.ActivationMode.CUTOVER:
                self.rpc("backend", "register_preparation")
                self.preparing = True
            else:
                self.rpc("backend", "ack_continuation")
        elif self.review.initial_service_state == "running" and self.review.activation_mode is not contract.ActivationMode.CUTOVER:
            raise ControlError("control_runtime_not_settled")
        if self.review.action is contract.ManagementAction.CONFIGURE_APPLY:
            self.store.prepare_revision(DeploymentRevision(self.review.installation_id, self.review.candidate.deployment_revision, self.review.deployment))
        self.advance(contract.ManagementStage.PREPARED)
        if self.review.action is contract.ManagementAction.CONFIGURE_APPLY:
            target = DeploymentRevision(self.review.installation_id, self.review.candidate.deployment_revision, self.review.deployment)
            self.store.publish_desired(ROOT, self.journal, lease=self.lease, expected=self.review.expected_desired, target=target.reference)
        self.wait_os_receipt()
        if self.review.activation_mode is not contract.ActivationMode.CUTOVER:
            self.advance(contract.ManagementStage.SAVED if self.review.activation_mode is contract.ActivationMode.SAVE_ONLY else contract.ManagementStage.OS_COMPLETED)
            self.complete()
            return
        if self.review.supervisor is None:
            raise ControlError("control_supervisor_start_required")
        if self.preparing:
            self.rpc("backend", "release_preparation")
            self.preparing = False
            if self.rpc("backend", "drain").get("drained") is not True:
                raise ControlError("control_drain_timeout")
        self.phase(state.LifecyclePhase.RESERVED, state.LifecyclePhase.EXCLUSIVE)
        self.advance(contract.ManagementStage.EXCLUSIVE)
        if self.rpc("supervisor", "stop_children").get("stopped") is not True:
            raise ControlError("control_children_busy")
        def assert_stopped():
            if self.rpc("supervisor", "assert_stopped").get("stopped") is not True:
                raise ControlError("control_children_busy")
        bind_management_runtime(ROOT, self.journal, lease=self.lease, assert_children_stopped=assert_stopped)
        self.phase(state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING)
        self.advance(contract.ManagementStage.VERIFYING)
        self.rpc("supervisor", "launch_candidate")
        evidence = self.wait_guarded()
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.advance(contract.ManagementStage.ACCEPTED, acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.journal.candidate), digest))
        publish_accepted_execution(ROOT, self.journal, lease=self.lease)
        if self.review.action is contract.ManagementAction.CONFIGURE_APPLY:
            self.store.promote_active(ROOT, self.journal, lease=self.lease)
        self.commit_and_wait_recovery()
        self.advance(contract.ManagementStage.SUCCEEDED)
        self.complete()

    def retain_recovery(self) -> None:
        if self.lease is None or self.journal is None:
            return
        self.lease.require_current()
        authority = state.inspect_state(ROOT)
        operation = authority.active_operation
        if operation is None or operation.operation_id != self.journal.operation_id or authority.generation != self.journal.generation:
            return
        if operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED:
            state.transition_operation_with_credential(ROOT, operation_id=self.journal.operation_id, generation=self.journal.generation,
                expected_phase=operation.phase, next_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
        updated = replace(self.journal, stage=contract.ManagementStage.RECOVERY_REQUIRED)
        self.journal = management_store.advance_management_journal(ROOT, self.journal, updated, lease=self.lease)

    def close(self) -> None:
        self.store.close()
        if self.lease is not None:
            self.lease.release()


def main(operation_id: str, *, control_manifest_sha256: str) -> int:
    from .control.recovery_requests import read_grant, recovery_path
    authority = state.inspect_state(ROOT)
    next_path = recovery_path(operation_id, max(2, authority.generation + 1))
    if authority.active_operation is not None and next_path.exists():
        controller = RecoveredManagementController(read_grant(operation_id, authority.generation + 1),
            control_manifest_sha256=control_manifest_sha256)
    else:
        reviewed = read_management_request(operation_id)
        controller = ManagementController(reviewed, control_manifest_sha256=control_manifest_sha256)
    def cancel(_signal, _frame):
        controller.cancelled = True
    for value in (signal.SIGTERM, signal.SIGINT):
        signal.signal(value, cancel)
    try:
        controller.execute()
        print("StreamHome management completed.")
        return 0
    except Exception:
        controller.retain_recovery()
        print("StreamHome management requires explicit recovery; retained evidence is authoritative.")
        return 2
    finally:
        controller.close()


class RecoveredManagementController(ManagementController):
    """Forward continuation after a fresh exact review, on the same supervisor."""
    def __init__(self, grant: dict, *, control_manifest_sha256: str):
        from .control.requests import parse_management_request
        from .control.recovery_requests import parse_grant
        self.grant = parse_grant(grant)
        super().__init__(parse_management_request(grant['request']), control_manifest_sha256=control_manifest_sha256)

    def execute(self) -> None:
        from .management_recovery_store import acquire_recovery_lease, resume_candidate_verification
        from .runtime_links import recover_management_links
        self.checkpoint()
        self.lease, self.claim, self.journal = acquire_recovery_lease(ROOT,
            operation_id=self.review.operation_id, generation=self.review.expected_generation,
            evidence_sha256=self.grant['source_evidence_sha256'], review_id=self.grant['recovery_review_id'],
            action=self.grant['action'], controller_invocation_id=self.invocation, pre_transfer_check=self.checkpoint)
        if self.journal.action is contract.ManagementAction.CONFIGURE_APPLY:
            self.store.rebind_apply_receipt(ROOT, self.journal, lease=self.lease)
        self.wait_os_receipt()
        if self.rpc('supervisor', 'recover_children').get('stopped') is not True:
            raise ControlError('control_children_busy')
        def assert_stopped():
            if self.rpc('supervisor', 'assert_recovery_stopped').get('stopped') is not True:
                raise ControlError('control_children_busy')
        recover_management_links(ROOT, self.journal, lease=self.lease, target='candidate', assert_children_stopped=assert_stopped)
        self.phase(state.LifecyclePhase.RECOVERY_REQUIRED, state.LifecyclePhase.EXCLUSIVE)
        self.phase(state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING)
        self.journal = resume_candidate_verification(ROOT, self.journal, lease=self.lease)
        self.rpc('supervisor', 'launch_recovered')
        evidence = self.wait_guarded()
        if self.journal.acceptance is None:
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            self.advance(contract.ManagementStage.ACCEPTED,
                acceptance=contract.AcceptanceEvidence(contract.execution_tuple_sha256(self.journal.candidate), digest))
        publish_accepted_execution(ROOT, self.journal, lease=self.lease)
        if self.journal.action is contract.ManagementAction.CONFIGURE_APPLY:
            self.store.promote_active(ROOT, self.journal, lease=self.lease)
        self.commit_and_wait_recovery()
        self.advance(contract.ManagementStage.SUCCEEDED)
        self.complete()
