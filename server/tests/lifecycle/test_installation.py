from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import installation


class InstallationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.descriptor = installation.InstallationDescriptor(
            str(uuid.uuid4()), installation.InstallationStatus.READY, 1001, 1001,
        )
        self.payload = self.descriptor.payload()
        self.path = self.directory / "install.json"

    def tearDown(self) -> None:
        os.close(self.fd)
        self.temporary.cleanup()

    def write(self, raw: bytes | None = None) -> bytes:
        data = json.dumps(self.payload).encode() if raw is None else raw
        self.path.write_bytes(data)
        self.path.chmod(0o644)
        return data

    def read(self) -> installation.DescriptorSnapshot | None:
        return installation.read_descriptor_at(self.fd, owner_uid=os.getuid())

    def test_ready_and_provisional_descriptors_round_trip(self) -> None:
        for status in installation.InstallationStatus:
            with self.subTest(status=status):
                descriptor = replace(self.descriptor, status=status)
                self.assertEqual(
                    installation.parse_descriptor(json.dumps(descriptor.payload()).encode()),
                    descriptor,
                )

    def test_fixed_paths_do_not_select_media_or_legacy_data(self) -> None:
        paths = self.descriptor.paths
        self.assertEqual(paths.database, Path("/var/lib/streamhome/database.db"))
        self.assertEqual(paths.lifecycle, Path("/var/lib/streamhome/lifecycle"))
        self.assertEqual(paths.runtime, Path("/run/streamhome"))
        self.assertFalse(hasattr(paths, "media"))
        for value in ("/tmp/other", "/opt/streamhome/", "/opt/../opt/streamhome"):
            with self.subTest(value=value):
                payload = self.descriptor.payload()
                payload["paths"]["application"] = value
                with self.assertRaises(installation.InstallationEvidenceError):
                    installation.parse_descriptor(json.dumps(payload).encode())

    def test_unknown_missing_and_unsafe_identity_fields_fail_closed(self) -> None:
        for changes in (
            {"unknown": "sensitive-value"}, {"schema_version": True}, {"schema_version": 2},
            {"layout_version": 2}, {"status": "installed"}, {"status": {}},
            {"service_uid": 0}, {"service_gid": 0}, {"service_uid": True},
            {"service_gid": -1}, {"service_uid": 2**32 - 1},
            {"installation_id": "not-a-uuid"}, {"installation_id": str(uuid.uuid4()).upper()},
            {"paths": []},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(installation.InstallationEvidenceError):
                    installation.parse_descriptor(json.dumps({**self.payload, **changes}).encode())
        for field in self.payload:
            payload = {key: value for key, value in self.payload.items() if key != field}
            with self.subTest(missing=field):
                with self.assertRaises(installation.InstallationEvidenceError):
                    installation.parse_descriptor(json.dumps(payload).encode())

    def test_duplicate_json_keys_and_nonstandard_numbers_are_rejected(self) -> None:
        valid = json.dumps(self.payload)
        invalid = (
            valid.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1'),
            valid.replace('"application":', '"application": "ignored", "application":'),
            valid.replace('"schema_version": 1', '"schema_version": NaN'),
            valid.replace('"schema_version": 1', '"schema_version": ' + "9" * 5000),
        )
        for value in invalid:
            with self.subTest(value=value[:80]):
                with self.assertRaises(installation.InstallationEvidenceError):
                    installation.parse_descriptor(value.encode())

    def test_malformed_bounded_json_is_rejected(self) -> None:
        for raw in (b"", b"{", b"[]", b"\xff", b" " * 8193, b"[" * 1200 + b"]" * 1200):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(installation.InstallationEvidenceError):
                    installation.parse_descriptor(raw)

    def test_absence_is_read_only_and_does_not_claim_no_legacy_install(self) -> None:
        self.assertIsNone(self.read())
        self.assertEqual(list(self.directory.iterdir()), [])
        output = io.StringIO()
        with patch.object(installation, "inspect_installation", return_value=None), redirect_stdout(output):
            self.assertEqual(installation.main(["inspect"]), 4)
        self.assertEqual(json.loads(output.getvalue()), {
            "ok": True, "managed": False, "legacy_discovery_required": True,
        })

    def test_actual_read_preserves_bytes_and_returns_snapshot_identity(self) -> None:
        raw = self.write()
        metadata = self.path.stat()
        snapshot = self.read()
        self.assertEqual(snapshot.descriptor, self.descriptor)
        self.assertEqual(snapshot.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"install.json"})

    def test_owner_default_cannot_trust_a_service_owned_file(self) -> None:
        self.write()
        with self.assertRaises(installation.InstallationEvidenceError):
            installation.read_descriptor_at(self.fd, owner_uid=os.getuid() + 1)

    def test_writable_or_executable_evidence_is_rejected(self) -> None:
        self.write()
        for mode in (0o664, 0o666, 0o755, 0o4644):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaises(installation.InstallationEvidenceError):
                    self.read()

    def test_writable_directory_is_rejected(self) -> None:
        self.write()
        self.directory.chmod(0o777)
        try:
            with self.assertRaises(installation.InstallationEvidenceError):
                self.read()
        finally:
            self.directory.chmod(0o700)

    def test_symlink_hardlink_directory_and_fifo_are_rejected(self) -> None:
        original = self.directory / "original.json"
        original.write_bytes(json.dumps(self.payload).encode())
        self.path.symlink_to(original)
        with self.assertRaises(installation.InstallationEvidenceError):
            self.read()
        self.path.unlink()
        os.link(original, self.path)
        with self.assertRaises(installation.InstallationEvidenceError):
            self.read()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(installation.InstallationEvidenceError):
            self.read()
        self.path.rmdir()
        os.mkfifo(self.path)
        with self.assertRaises(installation.InstallationEvidenceError):
            self.read()

    def test_atomic_replacement_during_read_is_rejected(self) -> None:
        self.write()
        replacement = self.directory / "next.json"
        replacement.write_bytes(json.dumps(self.payload).encode())
        real_read = os.read

        def swap(fd: int, length: int) -> bytes:
            result = real_read(fd, length)
            if replacement.exists():
                os.replace(replacement, self.path)
            return result

        with patch.object(installation.os, "read", side_effect=swap):
            with self.assertRaises(installation.InstallationEvidenceError) as context:
                self.read()
        self.assertEqual(context.exception.code, "descriptor_changed")

    def test_oversized_actual_file_is_rejected(self) -> None:
        self.write(b" " * 8193)
        with self.assertRaises(installation.InstallationEvidenceError):
            self.read()

    def test_help_does_not_inspect_or_mutate_an_installation(self) -> None:
        with patch.object(installation, "inspect_installation") as inspect, redirect_stdout(io.StringIO()):
            self.assertEqual(installation.main([]), 0)
        inspect.assert_not_called()

    def test_unsupported_platform_never_opens_installation_paths(self) -> None:
        with patch.object(installation.sys, "platform", "darwin"), patch.object(installation.os, "open") as opened:
            with self.assertRaises(installation.InstallationEvidenceError) as context:
                installation.inspect_installation()
        self.assertEqual(context.exception.code, "unsupported_host")
        opened.assert_not_called()

    def test_unreadable_root_is_a_sanitized_inspection_error(self) -> None:
        with patch.object(installation.os, "open", side_effect=PermissionError("private-path")):
            with self.assertRaises(installation.InstallationEvidenceError) as context:
                installation.inspect_installation()
        self.assertEqual(context.exception.code, "untrusted_descriptor")
        self.assertNotIn("private-path", str(context.exception))

    def test_ready_provisional_and_error_exit_statuses(self) -> None:
        for status, code in ((installation.InstallationStatus.READY, 0), (installation.InstallationStatus.PROVISIONING, 3)):
            snapshot = installation.DescriptorSnapshot(replace(self.descriptor, status=status), "a" * 64)
            with self.subTest(status=status), patch.object(installation, "inspect_installation", return_value=snapshot), redirect_stdout(io.StringIO()):
                self.assertEqual(installation.main(["inspect"]), code)
        output = io.StringIO()
        error = installation.InstallationEvidenceError("untrusted_descriptor", "Unsafe evidence.")
        with patch.object(installation, "inspect_installation", side_effect=error), redirect_stdout(output):
            self.assertEqual(installation.main(["inspect"]), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "untrusted_descriptor")

    def test_import_does_not_load_application_or_write_files(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(repository / "server")}
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import lifecycle.installation; "
             "assert {'config', 'db', 'main', 'application', 'models'}.isdisjoint(sys.modules); "
             "assert not any(n.startswith(('services.', 'routes.')) for n in sys.modules)"],
            cwd=self.directory, env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_managed_port_policy_rejects_malformed_or_reserved_inputs(self) -> None:
        for value in (False, None, 3000.0, " 3000", "+3000", "3e3", "３０００", "", 0, 80, 1023, 8000, "8000", 65536, "123456"):
            with self.subTest(value=value):
                with self.assertRaises(installation.InstallationEvidenceError):
                    installation.validate_managed_port(value)
        for value in (1024, "3000", "03000", 65535):
            with self.subTest(value=value):
                self.assertEqual(installation.validate_managed_port(value), int(value))


if __name__ == "__main__":
    unittest.main()
