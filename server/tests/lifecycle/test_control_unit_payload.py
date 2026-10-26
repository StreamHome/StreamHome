"""Data-only signed unit parsing and preservation of the fixed execution ABI."""
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.control import unit_payload, units
from lifecycle.control.rpc import ControlError


class UnitPayloadTests(unittest.TestCase):
    def setUp(self):
        self.raw=Path(units.__file__).read_bytes()

    def test_actual_six_unit_definitions_match_without_module_execution(self):
        self.assertEqual(unit_payload.parse(self.raw),units.unit_files())

    def test_unknown_module_statements_calls_or_duplicate_definitions_are_refused(self):
        for extra in (b'\nimport os\n',b'\nraise RuntimeError()\n',
                b'\nRUNTIME_UNIT = "replacement"\n', b'\ndef unit_files():\n return {}\n'):
            with self.subTest(extra=extra),self.assertRaises(ControlError):
                unit_payload.parse(self.raw+extra)
        for replacement in (b'open("must-not-execute")', b'RUNTIME_UNIT.upper()',
                b'RUNTIME_UNIT.replace("", "x")'):
            with self.subTest(replacement=replacement),self.assertRaises(ControlError):
                unit_payload.parse(self.raw.replace(b'RUNTIME_UNIT.encode()',replacement))

    def test_missing_duplicate_or_unapproved_destinations_fail(self):
        for raw in (self.raw.replace(b'/etc/systemd/system/streamhome.service',b'/etc/cron.d/streamhome'),
                self.raw.replace(b'/etc/systemd/system/streamhome-operation@.service',b'/etc/systemd/system/streamhome.service'),
                self.raw.replace(b'RUNTIME_UNIT.encode()',b'"".encode()')):
            with self.subTest(raw=raw[:40]),self.assertRaises(ControlError):unit_payload.parse(raw)

    def test_expansion_and_source_size_are_bounded_before_allocating(self):
        with self.assertRaises(ControlError):unit_payload.parse(b'x'*32769)
        with self.assertRaises(ControlError):
            unit_payload.parse(self.raw+b'\nTOO_BIG = RUNTIME_UNIT.replace("e", "'+b'x'*2000+b'")\n')

    def test_description_and_comment_changes_preserve_all_execution_directives(self):
        before=unit_payload.parse(self.raw)
        after=unit_payload.parse(self.raw.replace(b'StreamHome runtime supervisor',b'StreamHome managed runtime')
            .replace(b'[Unit]\n',b'[Unit]\n# Publisher unit revision\n'))
        self.assertNotEqual(before,after)
        unit_payload.compatible(before,after)

    def test_security_exec_order_duplicate_and_polkit_changes_are_incompatible(self):
        before=unit_payload.parse(self.raw)
        for old,new in ((b'User=streamhome',b'User=root'),(b'--supervisor',b'--cli start'),
                (b'ProtectHome=yes',b'ProtectHome=no'),(b'After=network-online.target',b'After=other.target'),
                (b'Restart=no',b'Restart=no\nRestart=always'),(b'polkit.Result.YES',b'polkit.Result.NO')):
            with self.subTest(old=old),self.assertRaises(ControlError):
                unit_payload.compatible(before,unit_payload.parse(self.raw.replace(old,new)))

    def test_description_cannot_hide_a_continued_directive(self):
        before=unit_payload.parse(self.raw)
        after=dict(before)
        name='/etc/systemd/system/streamhome.service'
        after[name]=after[name].replace(b'Description=StreamHome runtime supervisor\n',
            b'Description=StreamHome runtime supervisor\\\nExecStart=/bin/false\n')
        with self.assertRaises(ControlError):unit_payload.compatible(before,after)

    def test_owned_file_verification_reads_real_bytes_modes_and_aliases_at_all_fixed_paths(self):
        from lifecycle.control import administration, root_files
        expected=units.unit_files()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            paths={}
            for name,raw in expected.items():
                path=root/Path(name).name;path.write_bytes(raw);path.chmod(0o644);paths[name]=path
            original=os.fstat
            def metadata(fd):
                actual=original(fd)
                return SimpleNamespace(**{**{name:getattr(actual,name) for name in dir(actual) if name.startswith('st_')},'st_uid':0})
            with patch.object(root_files,'root_directory',side_effect=lambda path:os.open(root,os.O_RDONLY|os.O_DIRECTORY)), \
                    patch.object(administration.os,'fstat',side_effect=metadata), \
                    patch.object(administration,'systemctl',return_value='') as systemctl:
                administration._verify_owned_units(expected=expected)
                self.assertEqual(systemctl.call_count,5)
                target=paths['/etc/systemd/system/streamhome.service']
                target.write_bytes(b'unknown local edit')
                with self.assertRaises(ControlError):administration._verify_owned_units(expected=expected)
                target.write_bytes(expected['/etc/systemd/system/streamhome.service']);target.chmod(0o666)
                with self.assertRaises(ControlError):administration._verify_owned_units(expected=expected)
                target.chmod(0o644)
                retained=target.with_suffix('.retained');target.rename(retained);target.symlink_to(retained)
                with self.assertRaises(OSError):administration._verify_owned_units(expected=expected)

    def test_every_service_template_rejects_unqualified_dropins(self):
        from lifecycle.control import administration, root_files
        expected=units.unit_files()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name,raw in expected.items():
                path=root/Path(name).name;path.write_bytes(raw);path.chmod(0o644)
            original=os.fstat
            def metadata(fd):
                actual=original(fd)
                return SimpleNamespace(**{**{name:getattr(actual,name) for name in dir(actual) if name.startswith('st_')},'st_uid':0})
            with patch.object(root_files,'root_directory',side_effect=lambda path:os.open(root,os.O_RDONLY|os.O_DIRECTORY)), \
                    patch.object(administration.os,'fstat',side_effect=metadata):
                for name in expected:
                    if not name.endswith('.service'):continue
                    probe=Path(name).name.replace('@.service','@00000000-0000-0000-0000-000000000001.service')
                    def show(*args):return 'unqualified.conf' if args[1]==probe else ''
                    with self.subTest(name=name),patch.object(administration,'systemctl',side_effect=show),self.assertRaises(ControlError):
                        administration._verify_owned_units(expected=expected)


if __name__=='__main__':unittest.main()
