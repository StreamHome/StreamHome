"""Private immutable deployment revisions and domain-specific manifest CAS.

This module does not admit callers, claim operations, settle live processes or
activate a deployment. Deployment publication is subordinate to the exact
persisted MANAGEMENT journal and controller lease. Production provisioning must
create the initial manifest before opening this store; missing data is an error.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from typing import Any, Iterator, Mapping

from . import management_journal as management, management_store, state
from .deployment_config import DeploymentConfig, parse_deployment_payload


CONFIGURATION_SCHEMA_VERSION = 1
MAX_CONFIGURATION_BYTES = 32 * 1024
_DOMAIN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ConfigurationStoreError(ValueError):
    pass


class ConfigurationConflictError(ConfigurationStoreError):
    pass


class ConfigurationReceiptMissingError(ConfigurationStoreError):
    pass


@dataclass(frozen=True)
class DeploymentReference:
    revision: str
    sha256: str

    def payload(self) -> dict[str, str]:
        return {"revision": self.revision, "sha256": self.sha256}


@dataclass(frozen=True)
class ConfigurationManifest:
    installation_id: str
    active: DeploymentReference
    desired: DeploymentReference
    quarantined: tuple[str, ...]
    policy_revisions: tuple[tuple[str, int], ...]
    provider_revisions: tuple[tuple[str, int], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIGURATION_SCHEMA_VERSION,
            "installation_id": self.installation_id,
            "deployment": {
                "active": self.active.payload(), "desired": self.desired.payload(),
                "quarantined": list(self.quarantined),
            },
            "policy_revisions": dict(self.policy_revisions),
            "provider_revisions": dict(self.provider_revisions),
        }


@dataclass(frozen=True)
class DeploymentRevision:
    installation_id: str
    revision: str
    settings: DeploymentConfig

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIGURATION_SCHEMA_VERSION,
            "installation_id": self.installation_id,
            "revision": self.revision,
            "settings": self.settings.payload(),
        }

    @property
    def reference(self) -> DeploymentReference:
        return DeploymentReference(self.revision, hashlib.sha256(_encode(self.payload())).hexdigest())


@dataclass(frozen=True)
class ConfigurationApplyReceipt:
    installation_id: str
    operation_id: str
    generation: int
    journal_identity_sha256: str
    expected_desired: DeploymentReference
    previous_active: DeploymentReference
    target: DeploymentReference
    stage: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIGURATION_SCHEMA_VERSION,
            "installation_id": self.installation_id,
            "operation_id": self.operation_id,
            "generation": self.generation,
            "journal_identity_sha256": self.journal_identity_sha256,
            "expected_desired": self.expected_desired.payload(),
            "previous_active": self.previous_active.payload(),
            "target": self.target.payload(),
            "stage": self.stage,
        }


def _uuid(value: Any) -> str:
    try:
        return management._uuid(value, "configuration identity")
    except management.ManagementJournalError as exc:
        raise ConfigurationStoreError(str(exc)) from exc


def _object(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ConfigurationStoreError("Configuration contains missing or unsupported fields.")
    return value


def _version(value: Any) -> None:
    if type(value) is not int or value != CONFIGURATION_SCHEMA_VERSION:
        raise ConfigurationStoreError("The configuration format is unsupported.")


def _reference(value: Any) -> DeploymentReference:
    data = _object(value, {"revision", "sha256"})
    if not isinstance(data["sha256"], str) or not _HASH.fullmatch(data["sha256"]):
        raise ConfigurationStoreError("The configuration digest is invalid.")
    return DeploymentReference(_uuid(data["revision"]), data["sha256"])


def _domains(value: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict) or len(value) > 16:
        raise ConfigurationStoreError("The configuration domains are invalid.")
    for name, revision in value.items():
        if not isinstance(name, str) or not _DOMAIN.fullmatch(name) or type(revision) is not int or not 1 <= revision < 2**63:
            raise ConfigurationStoreError("The configuration domain revision is invalid.")
    return tuple(sorted(value.items()))


def parse_manifest(payload: Any) -> ConfigurationManifest:
    data = _object(payload, {"schema_version", "installation_id", "deployment", "policy_revisions", "provider_revisions"})
    _version(data["schema_version"])
    deployment = _object(data["deployment"], {"active", "desired", "quarantined"})
    quarantined = deployment["quarantined"]
    if not isinstance(quarantined, list) or len(quarantined) > 128:
        raise ConfigurationStoreError("The deployment quarantine is invalid.")
    identities = tuple(_uuid(value) for value in quarantined)
    if len(set(identities)) != len(identities):
        raise ConfigurationStoreError("The deployment quarantine contains duplicates.")
    result = ConfigurationManifest(
        _uuid(data["installation_id"]), _reference(deployment["active"]), _reference(deployment["desired"]),
        tuple(sorted(identities)), _domains(data["policy_revisions"]), _domains(data["provider_revisions"]),
    )
    if result.active.revision in identities:
        raise ConfigurationStoreError("The active deployment cannot be quarantined.")
    if result.active.revision == result.desired.revision and result.active != result.desired:
        raise ConfigurationStoreError("One immutable revision cannot have two digests.")
    return result


def parse_revision(payload: Any) -> DeploymentRevision:
    data = _object(payload, {"schema_version", "installation_id", "revision", "settings"})
    _version(data["schema_version"])
    return DeploymentRevision(_uuid(data["installation_id"]), _uuid(data["revision"]), parse_deployment_payload(data["settings"]))


def parse_apply_receipt(payload: Any) -> ConfigurationApplyReceipt:
    data = _object(payload, {
        "schema_version", "installation_id", "operation_id", "generation", "journal_identity_sha256",
        "expected_desired", "previous_active", "target", "stage",
    })
    _version(data["schema_version"])
    if (
        type(data["generation"]) is not int or not 1 <= data["generation"] < 2**63
        or not isinstance(data["journal_identity_sha256"], str) or not _HASH.fullmatch(data["journal_identity_sha256"])
        or not isinstance(data["stage"], str) or data["stage"] not in {"prepared", "applying", "saved", "accepted", "failed"}
    ):
        raise ConfigurationStoreError("The configuration apply receipt is invalid.")
    return ConfigurationApplyReceipt(
        _uuid(data["installation_id"]), _uuid(data["operation_id"]), data["generation"],
        data["journal_identity_sha256"], _reference(data["expected_desired"]),
        _reference(data["previous_active"]), _reference(data["target"]), data["stage"],
    )


def _journal_identity(journal: management.ManagementJournal) -> str:
    initial = replace(journal, stage=management.ManagementStage.RESERVED, acceptance=None)
    return hashlib.sha256(_encode(management.management_journal_payload(initial))).hexdigest()


def _encode(payload: Mapping[str, Any]) -> bytes:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    if len(raw) > MAX_CONFIGURATION_BYTES:
        raise ConfigurationStoreError("Configuration exceeds its persistence bound.")
    return raw


def _decode(raw: bytes) -> Any:
    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ConfigurationStoreError("Configuration contains duplicate fields.")
            data[key] = value
        return data

    def reject(value):
        raise ConfigurationStoreError("Configuration contains a nonstandard number.")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigurationStoreError("Configuration cannot be decoded.") from exc


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(metadata, name) for name in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns",
    ))


def _trusted(metadata: os.stat_result, *, directory: bool = False) -> None:
    correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not correct_type or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7077
        or not directory and (metadata.st_nlink != 1 or metadata.st_mode & 0o111)
    ):
        raise ConfigurationStoreError("Configuration ownership, type or permissions are unsafe.")


class ConfigurationStore:
    """Capability scoped to an already protected, service-owned directory FD.

    The provisioning/control caller resolves trusted ancestors; no environment
    variable or caller-selected path is interpreted as an installation here.
    """

    def __init__(self, directory_fd: int, installation_id: str) -> None:
        self.installation_id = _uuid(installation_id)
        _trusted(os.fstat(directory_fd), directory=True)
        self.fd = os.dup(directory_fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> ConfigurationStore:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _read(self, name: str) -> Any:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                _trusted(before)
                if before.st_size > MAX_CONFIGURATION_BYTES:
                    raise ConfigurationStoreError("Configuration exceeds its input bound.")
                raw = handle.read(MAX_CONFIGURATION_BYTES + 1)
                if len(raw) > MAX_CONFIGURATION_BYTES or _identity(before) != _identity(os.fstat(handle.fileno())):
                    raise ConfigurationStoreError("Configuration changed during inspection.")
                if _identity(before) != _identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False)):
                    raise ConfigurationStoreError("Configuration was replaced during inspection.")
            return _decode(raw)
        except OSError as exc:
            raise ConfigurationStoreError("Configuration is unavailable; explicit recovery is required.") from exc

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open("writer.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            _trusted(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _replace(self, name: str, payload: Mapping[str, Any], *, immutable: bool = False) -> None:
        raw = _encode(payload)
        temporary = ".configuration-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if immutable:
                os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=self.fd)
            else:
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except OSError as exc:
            raise ConfigurationStoreError("Configuration durability is uncertain; reconcile before retrying.") from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def inspect(self) -> ConfigurationManifest:
        manifest = parse_manifest(self._read("manifest.json"))
        if manifest.installation_id != self.installation_id:
            raise ConfigurationStoreError("Configuration belongs to another installation.")
        return manifest

    def initialize_provisional(self, root, revision: DeploymentRevision) -> ConfigurationManifest:
        """First-install publication under provisional identity and idle v2/gen0."""
        from .installation import InstallationStatus
        from .layout import layout_for_root
        layout = layout_for_root(root)
        if (not layout.managed or layout.descriptor.descriptor.installation_id != self.installation_id
                or layout.descriptor.descriptor.status is not InstallationStatus.PROVISIONING):
            raise ConfigurationStoreError("Initial configuration requires the exact provisional installation.")
        directory, opened = layout.domains.lstat(), os.fstat(self.fd)
        if (directory.st_dev, directory.st_ino) != (opened.st_dev, opened.st_ino) or not stat.S_ISDIR(directory.st_mode):
            raise ConfigurationStoreError("Initial configuration targets a different domain directory.")
        with state.hold_idle_authority(root) as authority:
            if authority.schema_version != 2 or authority.generation != 0:
                raise ConfigurationStoreError("Initial configuration cannot replace existing authority.")
            self.prepare_revision(revision)
            initial = ConfigurationManifest(self.installation_id, revision.reference, revision.reference, (), (), ())
            with self._lock():
                try:
                    os.stat("manifest.json", dir_fd=self.fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._replace("manifest.json", initial.payload(), immutable=True)
                else:
                    if self.inspect() != initial:
                        raise ConfigurationConflictError("Initial configuration already binds a different deployment.")
                    os.fsync(self.fd)
            return initial

    def read_revision(self, reference: DeploymentReference) -> DeploymentRevision:
        _reference(reference.payload())
        revision = parse_revision(self._read(f"deployment-{reference.revision}.json"))
        if revision.installation_id != self.installation_id or revision.reference != reference:
            raise ConfigurationStoreError("The immutable deployment does not match its manifest.")
        return revision

    def prepare_revision(self, revision: DeploymentRevision) -> DeploymentReference:
        """Durable inactive artifact only; never promotes desired or active state."""
        revision = parse_revision(revision.payload())
        if revision.installation_id != self.installation_id:
            raise ConfigurationStoreError("Deployment belongs to another installation.")
        with self._lock():
            name = f"deployment-{revision.revision}.json"
            try:
                existing = self._read(name)
            except ConfigurationStoreError:
                # Missing is the only condition that permits creation. Corrupt,
                # foreign, linked or otherwise unreadable evidence is retained.
                try:
                    os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._replace(name, revision.payload(), immutable=True)
                else:
                    raise
            else:
                if parse_revision(existing) != revision:
                    raise ConfigurationConflictError("This revision already binds different settings.")
                os.fsync(self.fd)
        return revision.reference

    def _require_management(self, root, journal, lease) -> None:
        lease.require_current()
        authority = state.inspect_state(root)
        management.validate_active_journal(journal, authority)
        if (
            self.installation_id != journal.installation_id
            or lease.paths.root != state.resolve_installation_root(root)
            or lease.lease.operation_id != journal.operation_id or lease.lease.generation != journal.generation
            or lease.lease.schema_version != authority.schema_version
            or lease.lease.owner_kind is not state.OwnerKind.MANAGEMENT_CONTROLLER
        ):
            raise state.OwnershipError("Configuration requires its exact MANAGEMENT lease and installation.")
        if management_store.inspect_management_journal(root, journal.operation_id) != journal:
            raise ConfigurationConflictError("The management journal changed after review.")

    def inspect_apply_receipt(self, operation_id: str, generation: int) -> ConfigurationApplyReceipt:
        _uuid(operation_id)
        if type(generation) is not int or generation <= 0:
            raise ConfigurationStoreError("The apply receipt generation is invalid.")
        receipt = parse_apply_receipt(self._read(f"apply-{operation_id}-{generation}.json"))
        if receipt.installation_id != self.installation_id or receipt.operation_id != operation_id or receipt.generation != generation:
            raise ConfigurationStoreError("The apply receipt does not match this installation/operation.")
        return receipt

    def _receipt_for_journal(self, journal) -> ConfigurationApplyReceipt:
        receipt = self.inspect_apply_receipt(journal.operation_id, journal.generation)
        if (
            receipt.journal_identity_sha256 != _journal_identity(journal)
            or receipt.previous_active.revision != journal.previous.deployment_revision
            or receipt.target.revision != journal.candidate.deployment_revision
            or journal.activation_mode is management.ActivationMode.SAVE_ONLY and receipt.stage in {"applying", "accepted", "failed"}
            or journal.activation_mode is management.ActivationMode.CUTOVER and receipt.stage == "saved"
        ):
            raise ConfigurationConflictError("The apply receipt does not bind the reviewed management inputs.")
        return receipt

    def _write_receipt(self, receipt: ConfigurationApplyReceipt) -> None:
        parse_apply_receipt(receipt.payload())
        self._replace(f"apply-{receipt.operation_id}-{receipt.generation}.json", receipt.payload())

    def rebind_apply_receipt(self, root, journal, *, lease, original_generation: int | None = None) -> ConfigurationApplyReceipt:
        """Copy exact retained inputs to a reviewed recovery generation.

        The old receipt is never edited. Missing or corrupt lineage cannot be
        replaced by inferring settings from the current manifest.
        """
        from .management_recovery_store import require_takeover_source
        if original_generation is not None and (type(original_generation) is not int or not 1 <= original_generation < journal.generation):
            raise ConfigurationStoreError("The original configure generation is invalid.")
        if journal.stage is not management.ManagementStage.RECOVERY_REQUIRED or journal.action is not management.ManagementAction.CONFIGURE_APPLY:
            raise ConfigurationStoreError("Receipt rebinding requires reviewed configure recovery.")
        with self._management_lock(root, journal, lease):
            source = require_takeover_source(root, journal)
            current = self.inspect()
            self._require_domains(current, journal)
            name = f"apply-{journal.operation_id}-{journal.generation}.json"
            try:
                os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                receipt = self._receipt_for_journal(journal)
                os.fsync(self.fd)
                return receipt
            for _ in range(128):
                previous_name = f"apply-{source.operation_id}-{source.generation}.json"
                try:
                    os.stat(previous_name, dir_fd=self.fd, follow_symlinks=False)
                except FileNotFoundError:
                    if source.generation == original_generation:
                        raise ConfigurationReceiptMissingError("The original configure never published its apply receipt.")
                    if original_generation is not None and source.generation < original_generation:
                        raise ConfigurationConflictError("The receipt lineage predates the original configure review.")
                    source = require_takeover_source(root, source)
                else:
                    receipt = self._receipt_for_journal(source)
                    break
            else:
                raise ConfigurationStoreError("The configuration receipt lineage exceeds its review bound.")
            if (current.active not in {receipt.previous_active, receipt.target}
                    or current.desired not in {receipt.expected_desired, receipt.target}
                    or journal.acceptance is None and current.active != receipt.previous_active
                    or current.active == receipt.target and journal.acceptance is not None and current.desired != receipt.target):
                raise ConfigurationConflictError("The manifest no longer matches the retained apply receipt.")
            self.read_revision(current.active)
            self.read_revision(current.desired)
            rebound = replace(receipt, generation=journal.generation, journal_identity_sha256=_journal_identity(journal))
            self._replace(name, rebound.payload(), immutable=True)
            return rebound

    def recover_desired(self, root, journal, *, lease, original_generation: int,
                        expected: DeploymentReference, target: DeploymentRevision) -> ConfigurationManifest:
        """Finish the original desired-publication intent during forward recovery.

        A missing original receipt permits creation only while both active and
        desired still equal the reviewed base. An unexplained target already in
        the manifest cannot be used to manufacture a lost receipt.
        """
        from .management_recovery_store import inspect_takeover
        if (journal.stage is not management.ManagementStage.RECOVERY_REQUIRED
                or journal.action is not management.ManagementAction.CONFIGURE_APPLY
                or journal.activation_mode not in {management.ActivationMode.CUTOVER, management.ActivationMode.SAVE_ONLY}
                or target.installation_id != self.installation_id
                or target.revision != journal.candidate.deployment_revision):
            raise ConfigurationStoreError("Desired recovery requires the original configure intent.")
        intent = inspect_takeover(root, journal.operation_id, journal.generation)
        if intent.action != 'continue_recovery':
            raise ConfigurationStoreError("The recovery review does not authorize desired publication.")
        self.prepare_revision(target)
        missing = False
        try:
            self.rebind_apply_receipt(root, journal, lease=lease, original_generation=original_generation)
        except ConfigurationReceiptMissingError:
            missing = True
        with self._management_lock(root, journal, lease):
            current = self.inspect()
            self._require_domains(current, journal)
            if missing:
                if (journal.acceptance is not None or current.desired != expected
                        or current.active.revision != journal.previous.deployment_revision):
                    raise ConfigurationConflictError("A missing receipt cannot explain an already published configuration.")
                receipt = self._intent(current, journal, expected, target.reference)
            else:
                receipt = self._receipt_for_journal(journal)
            if (receipt.expected_desired != expected or receipt.target != target.reference
                    or target.revision in current.quarantined
                    or current.desired not in {expected, target.reference}):
                raise ConfigurationConflictError("The original configure intent changed during recovery.")
            if journal.acceptance is not None:
                if receipt.stage not in {'applying', 'accepted'} or current.desired != target.reference:
                    raise ConfigurationConflictError("Accepted configure recovery requires its prior desired publication.")
                os.fsync(self.fd)
                return current
            outcome = 'saved' if journal.activation_mode is management.ActivationMode.SAVE_ONLY else 'applying'
            if current.active != receipt.previous_active or receipt.stage not in {'prepared', outcome}:
                raise ConfigurationConflictError("Unaccepted configure recovery cannot change the active revision.")
            self.read_revision(current.active)
            self.read_revision(current.desired)
            if current.desired != target.reference:
                current = replace(current, desired=target.reference)
                self._replace('manifest.json', current.payload())
            else:
                os.fsync(self.fd)
            self._write_receipt(replace(receipt, stage=outcome))
            return current

    def _intent(self, current, journal, expected, target) -> ConfigurationApplyReceipt:
        name = f"apply-{journal.operation_id}-{journal.generation}.json"
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            receipt = ConfigurationApplyReceipt(
                self.installation_id, journal.operation_id, journal.generation, _journal_identity(journal),
                expected, current.active, target, "prepared",
            )
            if current.desired != expected:
                raise ConfigurationConflictError("Deployment settings changed; review the latest revision.")
            self._write_receipt(receipt)
            return receipt
        receipt = self._receipt_for_journal(journal)
        if receipt.expected_desired != expected or receipt.target != target or receipt.previous_active != current.active:
            raise ConfigurationConflictError("The apply intent already binds different configuration inputs.")
        if receipt.stage not in {"prepared", "saved", "applying"}:
            raise ConfigurationConflictError("This apply intent has already reached a different terminal outcome.")
        return receipt

    def _require_domains(self, current, journal) -> None:
        if current.policy_revisions != journal.previous.policy_revisions or current.provider_revisions != journal.previous.provider_revisions:
            raise ConfigurationConflictError("A policy or provider revision changed after management review.")

    @contextmanager
    def _management_lock(self, root, journal, lease) -> Iterator[None]:
        with state.hold_operation_with_credential(
            root, operation_id=journal.operation_id, generation=journal.generation,
            expected_phase=management.journal_phase(journal), expected_operation_type=state.OperationType.MANAGEMENT,
        ):
            self._require_management(root, journal, lease)
            with self._lock():
                yield

    def publish_desired(
        self, root, journal: management.ManagementJournal, *, lease: state.ControllerLeaseHandle,
        expected: DeploymentReference, target: DeploymentReference,
    ) -> ConfigurationManifest:
        if journal.action is not management.ManagementAction.CONFIGURE_APPLY or journal.stage is not management.ManagementStage.PREPARED:
            raise ConfigurationStoreError("Desired publication requires a prepared configure operation.")
        if target.revision != journal.candidate.deployment_revision:
            raise ConfigurationConflictError("The target is not the reviewed candidate deployment.")
        with self._management_lock(root, journal, lease):
            current = self.inspect()
            self._require_domains(current, journal)
            if current.active.revision != journal.previous.deployment_revision:
                raise ConfigurationConflictError("The active deployment changed after review.")
            self.read_revision(current.active)
            self.read_revision(current.desired)
            if target.revision in current.quarantined:
                raise ConfigurationConflictError("The target deployment is quarantined.")
            self.read_revision(target)
            receipt = self._intent(current, journal, expected, target)
            if current.desired == target:
                os.fsync(self.fd)
                updated = current
            else:
                if current.desired != expected:
                    raise ConfigurationConflictError("Deployment settings changed; review the latest revision.")
                updated = replace(current, desired=target)
                self._replace("manifest.json", updated.payload())
            outcome = "saved" if journal.activation_mode is management.ActivationMode.SAVE_ONLY else "applying"
            self._write_receipt(replace(receipt, stage=outcome))
            return updated

    def promote_active(
        self, root, journal: management.ManagementJournal, *, lease: state.ControllerLeaseHandle,
    ) -> ConfigurationManifest:
        if (
            journal.action is not management.ManagementAction.CONFIGURE_APPLY
            or journal.activation_mode is not management.ActivationMode.CUTOVER
            or journal.stage is not management.ManagementStage.ACCEPTED
        ):
            raise ConfigurationStoreError("Active publication requires durable canonical candidate acceptance.")
        with self._management_lock(root, journal, lease):
            current = self.inspect()
            self._require_domains(current, journal)
            receipt = self._receipt_for_journal(journal)
            if receipt.stage not in {"applying", "accepted"}:
                raise ConfigurationConflictError("Desired publication has not been durably acknowledged.")
            target = current.desired
            if target != receipt.target or target.revision != journal.candidate.deployment_revision or target.revision in current.quarantined:
                raise ConfigurationConflictError("The accepted deployment is not the current eligible desired revision.")
            if current.active not in {receipt.previous_active, target}:
                raise ConfigurationConflictError("The active deployment changed after review.")
            self.read_revision(current.active)
            self.read_revision(target)
            if current.active == target:
                os.fsync(self.fd)
                updated = current
            else:
                updated = replace(current, active=target)
                self._replace("manifest.json", updated.payload())
            self._write_receipt(replace(receipt, stage="accepted"))
            return updated

    def quarantine_candidate(
        self, root, journal: management.ManagementJournal, *, lease: state.ControllerLeaseHandle,
    ) -> ConfigurationManifest:
        if (
            journal.action is not management.ManagementAction.CONFIGURE_APPLY
            or journal.activation_mode is not management.ActivationMode.CUTOVER
            or journal.stage is not management.ManagementStage.RECOVERY_REQUIRED
            or journal.acceptance is not None
            or journal.previous.deployment_revision == journal.candidate.deployment_revision
        ):
            raise ConfigurationStoreError("Only an unaccepted failed configure candidate can be quarantined.")
        with self._management_lock(root, journal, lease):
            current = self.inspect()
            self._require_domains(current, journal)
            receipt = self._receipt_for_journal(journal)
            if receipt.stage not in {"prepared", "applying", "failed"}:
                raise ConfigurationConflictError("An accepted deployment cannot be quarantined.")
            if (
                current.active != receipt.previous_active
                or current.desired != receipt.target
            ):
                raise ConfigurationConflictError("The failed deployment no longer matches the configuration manifest.")
            self.read_revision(current.active)
            self.read_revision(current.desired)
            if current.desired.revision in current.quarantined:
                os.fsync(self.fd)
                updated = current
            else:
                updated = replace(current, quarantined=tuple(sorted((*current.quarantined, current.desired.revision))))
                parse_manifest(updated.payload())
                self._replace("manifest.json", updated.payload())
            self._write_receipt(replace(receipt, stage="failed"))
            return updated

    def publish_domain_reference(
        self, root, *, domain: str, name: str, expected_revision: int | None,
        target_revision: int, expected_generation: int,
    ) -> ConfigurationManifest:
        """CAS a receipt from an already admitted policy/provider publisher.

        This only publishes an independently accepted domain reference. It does
        not validate/apply a policy or replace a provider's own transaction.
        """
        if domain not in {"policy", "provider"}:
            raise ConfigurationStoreError("Unknown configuration domain.")
        if type(expected_generation) is not int or expected_generation < 0:
            raise ConfigurationStoreError("The expected lifecycle generation is invalid.")
        _domains({name: target_revision})
        if expected_revision is not None:
            _domains({name: expected_revision})
        with state.hold_idle_authority(root) as authority:
            if authority.schema_version != state.MANAGEMENT_SCHEMA_VERSION or authority.generation != expected_generation:
                raise ConfigurationConflictError("The lifecycle generation changed before domain publication.")
            with self._lock():
                current = self.inspect()
                field = domain + "_revisions"
                revisions = dict(getattr(current, field))
                existing = revisions.get(name)
                if existing == target_revision:
                    os.fsync(self.fd)
                    return current
                if existing != expected_revision or target_revision <= (expected_revision or 0):
                    raise ConfigurationConflictError("The domain changed after review or the revision did not advance.")
                revisions[name] = target_revision
                updated = replace(current, **{field: tuple(sorted(revisions.items()))})
                self._replace("manifest.json", updated.payload())
                return updated
