"""Actual private-file publication and process-death recovery on disposable roots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import drive_connection as publication


class DriveConnectionPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="streamhome-drive-publication-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "server/rclone"
        self.directory.mkdir(parents=True, mode=0o700)
        self.settings = self.root / "server/settings.json"
        self.previous = b'{"storage_engine":"LOCAL","hevc_compression_mode":"off","custom":{"keep":true}}\n'
        self.settings.write_bytes(self.previous)
        self.candidate = self.directory / "encrypted-candidate"
        self.candidate.write_bytes(b"RCLONE_ENCRYPT_V0:\nprivate-synthetic-encrypted-content\n")
        self.config = self.directory / "rclone.conf"
        self.job_id = str(uuid.uuid4())
        self.arguments = dict(root=self.root, job_id=self.job_id, generation=1,
                              encrypted_config=self.candidate, remote_name="streamhome-drive",
                              remote_path="streamhome-drive:StreamHome-Test", audience="external",
                              publishing_status="production")

    def test_first_connection_preserves_storage_and_unrelated_preferences(self):
        receipt = publication.publish_drive_connection(**self.arguments)
        saved = json.loads(self.settings.read_bytes())
        self.assertEqual(saved["storage_engine"], "LOCAL")
        self.assertEqual(saved["hevc_compression_mode"], "off")
        self.assertEqual(saved["custom"], {"keep": True})
        self.assertEqual(saved["rclone_remote_path"], "streamhome-drive:StreamHome-Test")
        self.assertEqual(self.config.read_bytes(), self.candidate.read_bytes())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt["status"], "saved")
        self.assertEqual(publication.get_drive_connection_receipt(self.root, self.job_id, 1), receipt)
        self.assertIsNone(publication.get_drive_connection_receipt(self.root, self.job_id, 2))

    def test_existing_connection_cannot_be_overwritten(self):
        self.config.write_bytes(b"existing-configuration")
        with self.assertRaises(publication.DriveConnectionPublicationError) as error:
            publication.publish_drive_connection(**self.arguments)
        self.assertEqual(error.exception.code, "drive_already_configured")
        self.assertEqual(self.config.read_bytes(), b"existing-configuration")
        self.assertEqual(self.settings.read_bytes(), self.previous)

    def test_repeated_save_preserves_rotated_tokens_and_new_preferences(self):
        receipt = publication.publish_drive_connection(**self.arguments)
        self.config.write_bytes(b"RCLONE_ENCRYPT_V0:\nrotated-token-content\n")
        saved = json.loads(self.settings.read_bytes())
        saved["hevc_compression_mode"] = "on"
        self.settings.write_text(json.dumps(saved))
        self.assertEqual(publication.publish_drive_connection(**self.arguments), receipt)
        self.assertEqual(json.loads(self.settings.read_bytes())["hevc_compression_mode"], "on")
        self.assertIn(b"rotated-token", self.config.read_bytes())
        with self.assertRaises(publication.DriveConnectionPublicationError):
            publication.publish_drive_connection(**{**self.arguments, "job_id": str(uuid.uuid4())})

    def test_settings_io_failure_rolls_back_without_acceptance(self):
        atomic = publication._atomic_write
        def fail_settings(path, data):
            if path == self.settings:
                raise OSError("injected settings I/O failure")
            return atomic(path, data)
        with patch.object(publication, "_atomic_write", side_effect=fail_settings):
            with self.assertRaises(publication.DriveConnectionPublicationError):
                publication.publish_drive_connection(**self.arguments)
        self.assertEqual(self.settings.read_bytes(), self.previous)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.directory / publication.JOURNAL_NAME).exists())

    def _crash_at(self, target: str):
        code = """
import os, sys
from pathlib import Path
from lifecycle import drive_connection as p
root, job, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
atomic = p._atomic_write
def crash(path, data):
    atomic(path, data)
    if (target == 'config' and path.name == 'rclone.conf'
        or target == 'settings' and path.name == 'settings.json'
        or target == 'committed' and path.name == p.JOURNAL_NAME and b'"committed"' in data):
        os._exit(73)
p._atomic_write = crash
p.publish_drive_connection(root=root, job_id=job, generation=1,
    encrypted_config=root/'server/rclone/encrypted-candidate', remote_name='streamhome-drive',
    remote_path='streamhome-drive:StreamHome-Test', audience='external', publishing_status='production')
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root), self.job_id, target],
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        self.assertEqual(result.returncode, 73, result.stderr.decode())

    def test_process_death_after_config_publication_restores_previous_installation(self):
        self._crash_at("config")
        self.assertTrue(self.config.exists())
        self.assertIsNone(publication.recover_drive_connection(self.root))
        self.assertFalse(self.config.exists())
        self.assertEqual(self.settings.read_bytes(), self.previous)

    def test_process_death_after_settings_publication_does_not_claim_saved(self):
        self._crash_at("settings")
        with self.assertRaises(publication.DriveConnectionPublicationError):
            publication.get_drive_connection_receipt(self.root, self.job_id, 1)
        self.assertIsNone(publication.recover_drive_connection(self.root))
        self.assertEqual(self.settings.read_bytes(), self.previous)
        self.assertFalse(self.config.exists())

    def test_process_death_after_acceptance_recovers_saved_receipt(self):
        self._crash_at("committed")
        receipt = publication.recover_drive_connection(self.root)
        self.assertEqual(receipt["status"], "saved")
        self.assertEqual(receipt["jobId"], self.job_id)
        self.assertTrue(self.config.exists())
        self.assertEqual(publication.publish_drive_connection(**self.arguments), receipt)

    def test_ambiguous_pending_config_preserves_all_evidence(self):
        self._crash_at("settings")
        self.config.write_bytes(b"different-owner-content")
        observed = self.settings.read_bytes()
        with self.assertRaises(publication.DriveConnectionPublicationError):
            publication.recover_drive_connection(self.root)
        self.assertEqual(self.settings.read_bytes(), observed)
        self.assertEqual(self.config.read_bytes(), b"different-owner-content")
        self.assertTrue((self.directory / publication.JOURNAL_NAME).exists())

    def test_plaintext_or_symlink_candidate_is_rejected(self):
        self.candidate.write_bytes(b"[drive]\ntoken=synthetic\n")
        with self.assertRaises(publication.DriveConnectionPublicationError):
            publication.publish_drive_connection(**self.arguments)
        self.candidate.unlink()
        outside = self.root / "outside"
        outside.write_bytes(b"RCLONE_ENCRYPT_V0:\nsynthetic\n")
        self.candidate.symlink_to(outside)
        with self.assertRaises(publication.DriveConnectionPublicationError):
            publication.publish_drive_connection(**self.arguments)
        self.assertFalse(self.config.exists())

    def test_inspection_without_a_connection_does_not_write(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertIsNone(publication.get_drive_connection_receipt(self.root, self.job_id, 1))
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)

    def test_corrupt_journal_never_replaces_installation_files(self):
        journal = self.directory / publication.JOURNAL_NAME
        journal.write_bytes(b'{"phase":"prepared","phase":"committed"}')
        with self.assertRaises(publication.DriveConnectionPublicationError):
            publication.recover_drive_connection(self.root)
        self.assertEqual(self.settings.read_bytes(), self.previous)
        self.assertFalse(self.config.exists())

    def test_fifo_journal_is_rejected_without_blocking_startup(self):
        os.mkfifo(self.directory / publication.JOURNAL_NAME)
        code = """
import sys
from pathlib import Path
from lifecycle import drive_connection as p
try:
    p.recover_drive_connection(Path(sys.argv[1]))
except p.DriveConnectionPublicationError:
    sys.exit(0)
sys.exit(1)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.settings.read_bytes(), self.previous)

    def test_persistent_acceptance_sync_failure_cannot_be_reconciled_as_saved(self):
        original_sync = publication._sync
        def fail_after_commit(directory):
            journal = self.directory / publication.JOURNAL_NAME
            if journal.is_file() and json.loads(journal.read_bytes())["phase"] == "committed":
                raise OSError("injected acceptance fsync failure")
            return original_sync(directory)
        with patch.object(publication, "_sync", side_effect=fail_after_commit):
            with self.assertRaises(publication.DriveConnectionPublicationError):
                publication.publish_drive_connection(**self.arguments)
            with self.assertRaises(publication.DriveConnectionPublicationError):
                publication.get_drive_connection_receipt(self.root, self.job_id, 1)
        self.assertEqual(publication.recover_drive_connection(self.root)["status"], "saved")

    def test_oversized_serialized_settings_are_rejected_before_publication(self):
        compact = json.dumps({"custom": [0] * 400}, separators=(",", ":")).encode()
        self.settings.write_bytes(compact)
        with patch.object(publication, "MAX_FILE_BYTES", 1600):
            with self.assertRaises(publication.DriveConnectionPublicationError):
                publication.publish_drive_connection(**self.arguments)
        self.assertEqual(self.settings.read_bytes(), compact)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.directory / publication.JOURNAL_NAME).exists())


if __name__ == "__main__":
    unittest.main()
