from dataclasses import replace
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from lifecycle.control import reader_support as support
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import parse_execution_tuple
from tests.lifecycle.test_management_journal import _execution_payload


class PreviousReaderSupportTests(unittest.TestCase):
    def setUp(self):
        self.execution = parse_execution_tuple(_execution_payload())
        self.manifest = {'bundle_id': self.execution.control_bundle_id, 'support': {
            'schema_version': 3, 'platform': 'ubuntu-24.04-x86_64-glibc-python-3.12.3-systemd-cgroup2',
            'provisioning_journal_versions': [1, 2], 'management_journal_versions': [1, 2],
            'deadlines_seconds': {'startup_recovery': 900, 'startup_extension': 30, 'runtime_drain': 45,
                'runtime_stop': 60, 'controller': 3600, 'installation': 7200}}}

    def check(self, manifest, *, resumption=False):
        raw = json.dumps(manifest).encode()
        execution = replace(self.execution, control_manifest_sha256=hashlib.sha256(raw).hexdigest())
        with patch.object(support, '_protected'), patch.object(support, '_read', return_value=raw):
            return (support.require_resumption_reader if resumption else support.require_previous_reader)(execution)

    def test_resumption_requires_an_explicit_new_reader_declaration(self):
        with self.assertRaises(ControlError):self.check(self.manifest, resumption=True)
        current = {**self.manifest, 'support': {**self.manifest['support'], 'schema_version': 4,
            'recovery_request_versions': [1, 2, 3], 'resumption_plan_versions': [1]}}
        self.assertIsNone(self.check(current, resumption=True))
        self.assertIsNone(self.check(current))
        for change in ({'recovery_request_versions': [1, 2]}, {'resumption_plan_versions': [True]},
                       {'resumption_plan_versions': [1, 2]}):
            with self.subTest(change=change), self.assertRaises(ControlError):
                self.check({**current, 'support': {**current['support'], **change}}, resumption=True)

    def test_exact_protected_manifest_declares_previous_acceptance_format(self):
        self.assertIsNone(self.check(self.manifest))

    def test_old_or_incomplete_reader_is_not_accepted_by_guessing_compatibility(self):
        for changed in ({'schema_version': 2}, {'management_journal_versions': [1]},
                        {'management_journal_versions': [True, 2]}):
            manifest = {**self.manifest, 'support': {**self.manifest['support'], **changed}}
            if changed.get('schema_version') == 2:
                manifest['support'].pop('management_journal_versions')
            with self.subTest(changed=changed), self.assertRaisesRegex(ControlError, 'control_bound_reader_incompatible'):
                self.check(manifest)

    def test_missing_tampered_or_unprotected_manifest_does_not_authorize_rollback(self):
        raw = json.dumps(self.manifest).encode()
        with patch.object(support, '_protected'), patch.object(support, '_read', return_value=raw):
            with self.assertRaises(ControlError):
                support.require_previous_reader(self.execution)
        with patch.object(support, '_protected', side_effect=OSError('protected evidence unavailable')):
            with self.assertRaises(ControlError):
                support.require_previous_reader(self.execution)

    def test_reader_inspection_never_imports_canonical_or_application_modules(self):
        code = "import sys; import lifecycle.control.reader_support; assert not {'lifecycle.state','config','application','models','database'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
