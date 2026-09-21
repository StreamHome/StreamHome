from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from lifecycle import backup_manifest


class BackupManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "backup_v1_20260921_120000.db"
        self.database.write_bytes(b"standalone-sqlite-evidence")
        self.database.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create(self) -> backup_manifest.BackupManifest:
        return backup_manifest.create_manifest(
            self.database,
            application_build_id="abc123",
            created_at=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
        )

    def _payload(self) -> dict:
        return backup_manifest.manifest_payload(self._create())

    def _write_payload(self, payload: object, *, database: Path | None = None) -> Path:
        target = backup_manifest.manifest_path_for(database or self.database)
        target.write_text(json.dumps(payload), encoding="utf-8")
        target.chmod(0o600)
        return target

    def test_valid_manifest_backed_backup_is_exact_and_private(self) -> None:
        manifest = self._create()
        evidence = backup_manifest.validate_backup_pair(self.database)
        self.assertEqual(evidence.format, "manifest_v1")
        self.assertEqual(evidence.manifest, manifest)
        self.assertEqual(evidence.database.filename, self.database.name)
        self.assertEqual(
            backup_manifest.manifest_path_for(self.database).stat().st_mode & 0o777,
            0o600,
        )

    def test_manifest_and_recovery_imports_do_not_probe_or_write(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "import lifecycle.backup_manifest, lifecycle.recovery; "
                    "print(len(list(Path('.').iterdir())))"
                ),
            ],
            cwd=self.root,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            capture_output=True,
            text=True,
            check=True,
        )
        # Only the database created by setUp exists.
        self.assertEqual(result.stdout.strip(), "1")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        manifest = backup_manifest.manifest_path_for(self.database)
        identity = backup_manifest.database_identity(self.database, private=True)
        manifest.write_text(
            "{"
            '"schema_version":1,"schema_version":1,'
            '"backup_format":"streamhome-sqlite-standalone",'
            '"created_at":"2026-09-21T12:00:00Z",'
            '"application_build_id":"abc123",'
            '"database_contract_version":1,'
            f'"database":{{"filename":"{identity.filename}",'
            f'"size_bytes":{identity.size_bytes},"sha256":"{identity.sha256}"}}'
            "}",
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        with self.assertRaises(backup_manifest.BackupManifestError):
            backup_manifest.validate_backup_pair(self.database)

    def test_unsupported_schema_and_unknown_fields_are_rejected(self) -> None:
        payload = self._payload()
        path = backup_manifest.manifest_path_for(self.database)
        path.unlink()
        for change in (
            {"schema_version": 2},
            {"schema_version": 1.0},
            {"unknown": True},
            {"database_contract_version": 2},
            {"database_contract_version": 1.0},
        ):
            with self.subTest(change=change):
                candidate = {**payload, **change}
                self._write_payload(candidate)
                with self.assertRaises(backup_manifest.BackupManifestError):
                    backup_manifest.validate_backup_pair(self.database)
                path.unlink()

    def test_malformed_size_digest_timestamp_and_build_are_rejected(self) -> None:
        payload = self._payload()
        path = backup_manifest.manifest_path_for(self.database)
        path.unlink()
        changes = (
            {"created_at": "2026-09-21 12:00:00"},
            {"application_build_id": "../../private"},
            {"database": {**payload["database"], "size_bytes": True}},
            {"database": {**payload["database"], "size_bytes": 0}},
            {"database": {**payload["database"], "sha256": "A" * 64}},
        )
        for change in changes:
            with self.subTest(change=change):
                self._write_payload({**payload, **change})
                with self.assertRaises(backup_manifest.BackupManifestError):
                    backup_manifest.validate_backup_pair(self.database)
                path.unlink()

    def test_filename_hash_and_size_mismatch_fail_closed(self) -> None:
        payload = self._payload()
        path = backup_manifest.manifest_path_for(self.database)
        path.unlink()
        changes = (
            {"filename": "backup_v1_20260921_120001.db"},
            {"size_bytes": payload["database"]["size_bytes"] + 1},
            {"sha256": "0" * 64},
        )
        for change in changes:
            with self.subTest(change=change):
                self._write_payload(
                    {**payload, "database": {**payload["database"], **change}}
                )
                with self.assertRaises(backup_manifest.BackupManifestError):
                    backup_manifest.validate_backup_pair(self.database)
                path.unlink()

    def test_new_name_cannot_downgrade_when_manifest_is_missing(self) -> None:
        with self.assertRaises(backup_manifest.BackupManifestMissingError):
            backup_manifest.validate_backup_pair(self.database)

    def test_valid_legacy_backup_remains_supported(self) -> None:
        legacy = self.root / "backup_20260921_120000.db"
        legacy.write_bytes(b"legacy-standalone")
        evidence = backup_manifest.validate_backup_pair(legacy)
        self.assertEqual(evidence.format, "legacy")
        self.assertIsNone(evidence.manifest)

    def test_corrupt_manifest_beside_legacy_backup_is_not_ignored(self) -> None:
        legacy = self.root / "backup_20260921_120000.db"
        legacy.write_bytes(b"legacy-standalone")
        manifest = backup_manifest.manifest_path_for(legacy)
        manifest.write_text("{", encoding="utf-8")
        manifest.chmod(0o600)
        with self.assertRaises(backup_manifest.BackupManifestError):
            backup_manifest.validate_backup_pair(legacy)

    def test_symlink_database_and_manifest_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        linked_db = self.root / "backup_v1_20260921_120001.db"
        linked_db.symlink_to(outside)
        with self.assertRaises(backup_manifest.BackupManifestError):
            backup_manifest.validate_backup_pair(linked_db)

        self._create()
        manifest = backup_manifest.manifest_path_for(self.database)
        contents = manifest.read_bytes()
        manifest.unlink()
        outside_manifest = self.root / "outside-manifest"
        outside_manifest.write_bytes(contents)
        manifest.symlink_to(outside_manifest)
        with self.assertRaises(backup_manifest.BackupManifestError):
            backup_manifest.validate_backup_pair(self.database)

    def test_manifest_publication_failure_removes_partial_sidecar(self) -> None:
        real_fsync = os.fsync
        calls = 0

        def fail_directory_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory fsync failed")
            real_fsync(descriptor)

        with mock.patch.object(backup_manifest.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaises(backup_manifest.BackupManifestPersistenceError):
                self._create()
        self.assertFalse(backup_manifest.manifest_path_for(self.database).exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_pair_deletion_removes_database_before_manifest(self) -> None:
        self._create()
        manifest = backup_manifest.manifest_path_for(self.database)
        backup_manifest.delete_backup_pair(self.database)
        self.assertFalse(self.database.exists())
        self.assertFalse(manifest.exists())

    def test_incomplete_pair_deletion_never_leaves_new_database(self) -> None:
        with self.assertRaises(backup_manifest.BackupManifestPersistenceError):
            backup_manifest.delete_backup_pair(self.database)
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
