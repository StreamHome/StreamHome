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
    def test_exact_control_repair_requires_complete_new_management_and_original_owner_formats(self):
        current={**self.manifest,'support':{**self.manifest['support'],'schema_version':12,
            'recovery_request_versions':[1,2,3,4,5,6],'resumption_plan_versions':[1,2,3],
            'runtime_recovery_cache_versions':[1],'maintenance_actions':['repair_bound_environment','repair_bound_control'],
            'descriptor_handoff_versions':[1],'managed_restore_versions':[1],
            'managed_parent_recovery_versions':[1,2,3,4],'managed_parent_environment_repair_versions':[1],
            'managed_update_versions':[1],'managed_update_recovery_versions':[1,2,3,4],
            'managed_update_environment_repair_versions':[1],'control_maintenance_versions':[1],
            'bound_control_repair_versions':[1]}}
        for mode in ({'managed_update':True},{'parent_bound_repair':True},{'resumption':True}):
            with self.subTest(mode=mode):self.assertIsNone(self.check(current,**mode))
        connector={**current,'support':{**current['support'],'schema_version':13,'cloudflare_connector_versions':[1]}}
        for mode in ({'managed_update':True},{'parent_bound_repair':True},{'resumption':True}):
            with self.subTest(connector_mode=mode):self.assertIsNone(self.check(connector,**mode))
        for change in ({'cloudflare_connector_versions':[True]},{'cloudflare_connector_versions':[]},
                {'cloudflare_connector_versions':[1,2]}):
            with self.subTest(connector_change=change),self.assertRaises(ControlError):
                self.check({**connector,'support':{**connector['support'],**change}},managed_update=True)
        for change in ({'bound_control_repair_versions':[True]},{'recovery_request_versions':[1,2,3,4,5]},
                {'managed_parent_recovery_versions':[1,2,3]},{'managed_update_recovery_versions':[1,2,3]},
                {'maintenance_actions':['repair_bound_environment']},{'schema_version':11}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                self.check({**current,'support':{**current['support'],**change}},managed_update=True)

    def setUp(self):
        self.execution = parse_execution_tuple(_execution_payload())
        self.manifest = {'bundle_id': self.execution.control_bundle_id, 'support': {
            'schema_version': 3, 'platform': 'ubuntu-24.04-x86_64-glibc-python-3.12.3-systemd-cgroup2',
            'provisioning_journal_versions': [1, 2], 'management_journal_versions': [1, 2],
            'deadlines_seconds': {'startup_recovery': 900, 'startup_extension': 30, 'runtime_drain': 45,
                'runtime_stop': 60, 'controller': 3600, 'installation': 7200}}}

    def check(self, manifest, *, resumption=False, bound_repair=False, managed_restore=False, parent_recovery=False,
              parent_resumption=False, parent_bound_repair=False, managed_update=False):
        raw = json.dumps(manifest).encode()
        execution = replace(self.execution, control_manifest_sha256=hashlib.sha256(raw).hexdigest())
        with patch.object(support, '_protected'), patch.object(support, '_read', return_value=raw):
            return (support.require_managed_update_reader if managed_update else
                    support.require_parent_bound_repair_reader if parent_bound_repair else
                    support.require_parent_resumption_reader if parent_resumption else
                    support.require_parent_recovery_reader if parent_recovery else
                    support.require_managed_restore_reader if managed_restore else support.require_bound_repair_reader
                    if bound_repair else support.require_resumption_reader
                    if resumption else support.require_previous_reader)(execution)

    def test_restore_repair_reader_cannot_borrow_incomplete_managed_update_support(self):
        current = {**self.manifest, 'support': {**self.manifest['support'], 'schema_version': 9,
            'recovery_request_versions': [1, 2, 3, 4], 'resumption_plan_versions': [1, 2],
            'runtime_recovery_cache_versions': [1], 'maintenance_actions': ['repair_bound_environment'],
            'descriptor_handoff_versions': [1], 'managed_restore_versions': [1],
            'managed_parent_recovery_versions': [1, 2, 3],
            'managed_parent_environment_repair_versions': [1]}}
        self.assertIsNone(self.check(current, parent_bound_repair=True))
        with self.assertRaises(ControlError):self.check(current, managed_update=True)
        with self.assertRaises(ControlError):self.check({**current,
            'support': {**current['support'], 'managed_update_versions': [1]}}, managed_update=True)

    def test_managed_update_requires_complete_journal_recovery_resumption_and_bound_repair_contract(self):
        current = {**self.manifest, 'support': {**self.manifest['support'], 'schema_version': 10,
            'recovery_request_versions': [1,2,3,4], 'resumption_plan_versions': [1,2],
            'runtime_recovery_cache_versions': [1], 'maintenance_actions': ['repair_bound_environment'],
            'descriptor_handoff_versions': [1], 'managed_restore_versions': [1],
            'managed_parent_recovery_versions': [1,2,3], 'managed_parent_environment_repair_versions': [1],
            'managed_update_versions': [1], 'managed_update_recovery_versions': [1,2,3],
            'managed_update_environment_repair_versions': [1]}}
        self.assertIsNone(self.check(current, managed_update=True))
        self.assertIsNone(self.check(current, parent_bound_repair=True))
        self.assertIsNone(self.check(current, resumption=True))
        for changes in ({'managed_update_versions': [True]}, {'managed_update_recovery_versions': [1,2]},
                {'managed_update_environment_repair_versions': []}, {'resumption_plan_versions': [1]},
                {'managed_update_environment_repair_versions': [True]}, {'schema_version': 9}):
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                self.check({**current, 'support': {**current['support'], **changes}}, managed_update=True)

    def test_parent_operator_recovery_requires_explicit_bound_reader_support(self):
        current={**self.manifest,'support':{**self.manifest['support'],'schema_version':7,
            'recovery_request_versions':[1,2,3,4],'resumption_plan_versions':[1],
            'runtime_recovery_cache_versions':[1],'maintenance_actions':['repair_bound_environment'],
            'descriptor_handoff_versions':[1],'managed_restore_versions':[1],
            'managed_parent_recovery_versions':[1]}}
        self.assertIsNone(self.check(current,parent_recovery=True))
        self.assertIsNone(self.check(current,managed_restore=True))
        older={**current,'support':{k:v for k,v in current['support'].items()
            if k!='managed_parent_recovery_versions'}}
        older['support']['schema_version']=6
        with self.assertRaises(ControlError):self.check(older,parent_recovery=True)
        for versions in ([],[True],[1,2]):
            with self.subTest(versions=versions),self.assertRaises(ControlError):
                self.check({**current,'support':{**current['support'],
                    'managed_parent_recovery_versions':versions}},parent_recovery=True)

    def test_parent_resumption_requires_both_new_permission_formats(self):
        current={**self.manifest,'support':{**self.manifest['support'],'schema_version':8,
            'recovery_request_versions':[1,2,3,4],'resumption_plan_versions':[1,2],
            'runtime_recovery_cache_versions':[1],'maintenance_actions':['repair_bound_environment'],
            'descriptor_handoff_versions':[1],'managed_restore_versions':[1],
            'managed_parent_recovery_versions':[1,2]}}
        self.assertIsNone(self.check(current,parent_resumption=True))
        self.assertIsNone(self.check(current,parent_recovery=True))
        older={**current,'support':{**current['support'],'schema_version':7,
            'resumption_plan_versions':[1],'managed_parent_recovery_versions':[1]}}
        with self.assertRaises(ControlError):self.check(older,parent_resumption=True)
        for change in ({'resumption_plan_versions':[1]},{'managed_parent_recovery_versions':[1]},
                       {'managed_parent_recovery_versions':[True,2]}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                self.check({**current,'support':{**current['support'],**change}},parent_resumption=True)

    def test_management_repair_or_parent_resumption_support_cannot_authorize_parent_reconstruction(self):
        current={**self.manifest,'support':{**self.manifest['support'],'schema_version':8,
            'recovery_request_versions':[1,2,3,4],'resumption_plan_versions':[1,2],
            'runtime_recovery_cache_versions':[1],'maintenance_actions':['repair_bound_environment'],
            'descriptor_handoff_versions':[1],'managed_restore_versions':[1],
            'managed_parent_recovery_versions':[1,2]}}
        with self.assertRaises(ControlError):self.check(current,parent_bound_repair=True)
        # A field injected into an older contract cannot invent compatibility.
        with self.assertRaises(ControlError):self.check({**current,'support':{**current['support'],
            'managed_parent_environment_repair_versions':[1]}},parent_bound_repair=True)

    def test_parent_repair_requires_its_own_intent_and_grant_reader_declaration(self):
        current={**self.manifest,'support':{**self.manifest['support'],'schema_version':9,
            'recovery_request_versions':[1,2,3,4],'resumption_plan_versions':[1,2],
            'runtime_recovery_cache_versions':[1],'maintenance_actions':['repair_bound_environment'],
            'descriptor_handoff_versions':[1],'managed_restore_versions':[1],
            'managed_parent_recovery_versions':[1,2,3],
            'managed_parent_environment_repair_versions':[1]}}
        self.assertIsNone(self.check(current,parent_bound_repair=True))
        self.assertIsNone(self.check(current,parent_resumption=True))
        self.assertIsNone(self.check(current,parent_recovery=True))
        for change in ({'managed_parent_environment_repair_versions':[True]},
                       {'managed_parent_recovery_versions':[1,2]}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                self.check({**current,'support':{**current['support'],**change}},parent_bound_repair=True)

    def test_managed_restore_requires_explicit_fd_and_controller_compatibility(self):
        with self.assertRaises(ControlError):self.check(self.manifest,managed_restore=True)
        current={**self.manifest,'support':{**self.manifest['support'],'schema_version':6,
            'recovery_request_versions':[1,2,3,4],'resumption_plan_versions':[1],
            'runtime_recovery_cache_versions':[1],'maintenance_actions':['repair_bound_environment'],
            'descriptor_handoff_versions':[1],'managed_restore_versions':[1]}}
        self.assertIsNone(self.check(current,managed_restore=True))
        self.assertIsNone(self.check(current,bound_repair=True))
        self.assertIsNone(self.check(current,resumption=True))
        for change in ({'descriptor_handoff_versions':[True]},{'managed_restore_versions':[]},
                       {'managed_update_versions':[1]}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                self.check({**current,'support':{**current['support'],**change}},managed_restore=True)

    def test_bound_repair_requires_explicit_maintenance_and_grant_compatibility(self):
        with self.assertRaises(ControlError):self.check(self.manifest, bound_repair=True)
        current = {**self.manifest, 'support': {**self.manifest['support'], 'schema_version': 5,
            'recovery_request_versions': [1, 2, 3, 4], 'resumption_plan_versions': [1],
            'runtime_recovery_cache_versions': [1], 'maintenance_actions': ['repair_bound_environment']}}
        self.assertIsNone(self.check(current, bound_repair=True))
        self.assertIsNone(self.check(current, resumption=True))
        for change in ({'recovery_request_versions': [1, 2, 3]}, {'runtime_recovery_cache_versions': [True]},
                       {'maintenance_actions': ['repair_bound_environment', 'start']}):
            with self.subTest(change=change), self.assertRaises(ControlError):
                self.check({**current, 'support': {**current['support'], **change}}, bound_repair=True)

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
