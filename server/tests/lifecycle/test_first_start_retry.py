from copy import deepcopy
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle.control import provisioning_contract as contract
from lifecycle.control import first_start_retry as retry
from lifecycle.control import provision_worker
from lifecycle.control.entry import EntryError, qualified_support
from lifecycle.control.provisioning_mirror import _prefix
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_provisioning import prepared_intent


def failed_attempt():
    issued = contract.advance_intent(prepared_intent(), stage='first_start_requested', start_permit='issued',
        start_boot_id=str(uuid.uuid4()), start_stop_epoch='a' * 64)
    return contract.advance_intent(issued, start_permit='consumed', start_invocation='1' * 32)


class ReviewedFirstStartContractTests(unittest.TestCase):
    def test_control_support_declares_root_journal_formats_independently(self):
        support = {'schema_version': 1, 'platform': 'ubuntu-24.04-x86_64-glibc-python-3.12.3-systemd-cgroup2',
            'deadlines_seconds': {'startup_recovery': 900, 'startup_extension': 30, 'runtime_drain': 45, 'runtime_stop': 60, 'controller': 3600}}
        self.assertEqual(qualified_support(support), (1,))
        modern = {**support, 'schema_version': 2, 'provisioning_journal_versions': [1, 2],
            'deadlines_seconds': {**support['deadlines_seconds'], 'installation': 7200}}
        self.assertEqual(qualified_support(modern), (1, 2))
        management = {**modern, 'schema_version': 3, 'management_journal_versions': [1, 2]}
        self.assertEqual(qualified_support(management), (1, 2))
        for formats in ([1], [2], [True, 2], '1,2', None):
            with self.subTest(formats=formats), self.assertRaises(EntryError):
                qualified_support({**management, 'management_journal_versions': formats})
        for change in ({'provisioning_journal_versions': [2]}, {'provisioning_journal_versions': [True, 2]}, {'extra': 'unqualified'}):
            with self.subTest(change=change), self.assertRaises(EntryError):
                qualified_support({**modern, **change})

    def test_explicit_attempt_preserves_retired_permission_and_exact_installation(self):
        previous = failed_attempt()
        reviewed = contract.reviewed_start_attempt(previous, review_id=str(uuid.uuid4()),
            boot_id=str(uuid.uuid4()), stop_epoch='b' * 64)
        self.assertEqual(previous.payload()['schema_version'], 1)
        self.assertNotIn('start_attempts', previous.payload())
        self.assertEqual(reviewed.payload()['schema_version'], 2)
        self.assertEqual(contract.parse_intent(json.loads(json.dumps(reviewed.payload()))), reviewed)
        archived = reviewed.start_attempts[0]
        self.assertEqual(archived.previous_intent_sha256, contract.intent_sha256(previous))
        self.assertEqual((archived.permit, archived.invocation, archived.boot_id, archived.stop_epoch),
            (previous.start_permit, previous.start_invocation, previous.start_boot_id, previous.start_stop_epoch))
        for name in ('installation_id', 'operation_id', 'revision_id', 'application_commit', 'execution_sha256',
                     'deployment', 'directories', 'mirror_directories', 'control_bundle_id', 'control_manifest_sha256'):
            self.assertEqual(getattr(reviewed, name), getattr(previous, name))
        self.assertEqual(reviewed.start_permit, 'issued')
        self.assertIsNone(reviewed.start_invocation)
        with self.assertRaises(ControlError):
            contract.advance_intent(previous, start_attempts=reviewed.start_attempts,
                start_permit='issued', start_invocation=None, start_boot_id=reviewed.start_boot_id, start_stop_epoch=reviewed.start_stop_epoch)

    def test_mirror_reconciles_only_valid_reviewed_history_and_never_reverts_it(self):
        previous = failed_attempt()
        reviewed = contract.reviewed_start_attempt(previous, review_id=str(uuid.uuid4()), boot_id=previous.start_boot_id, stop_epoch='b' * 64)
        consumed = contract.advance_intent(reviewed, start_permit='consumed', start_invocation='2' * 32)
        next_review = contract.reviewed_start_attempt(consumed, review_id=str(uuid.uuid4()), boot_id=previous.start_boot_id, stop_epoch='c' * 64)
        self.assertTrue(_prefix(previous, next_review))
        self.assertTrue(_prefix(reviewed, next_review))
        self.assertTrue(_prefix(consumed, next_review))
        self.assertFalse(_prefix(next_review, consumed))
        self.assertFalse(_prefix(previous, replace(reviewed, execution_sha256='f' * 64)))
        forged = replace(reviewed, start_attempts=(replace(reviewed.start_attempts[0], invocation='3' * 32),))
        self.assertFalse(_prefix(previous, forged))

    def test_retired_invocation_source_and_review_cannot_be_rewritten(self):
        previous = failed_attempt()
        reviewed = contract.reviewed_start_attempt(previous, review_id=str(uuid.uuid4()), boot_id=previous.start_boot_id, stop_epoch='b' * 64)
        for key, value in (('invocation', '3' * 32), ('previous_intent_sha256', 'f' * 64), ('permit', [])):
            payload = deepcopy(reviewed.payload())
            payload['start_attempts'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ControlError):
                contract.parse_intent(payload)
        with self.assertRaises(ControlError):
            contract.reviewed_start_attempt(reviewed, review_id=reviewed.start_attempts[0].review_id,
                boot_id=previous.start_boot_id, stop_epoch='c' * 64)

    def test_ready_registration_can_never_be_reopened_as_a_first_attempt(self):
        registered = contract.advance_intent(failed_attempt(), stage='registered')
        with self.assertRaisesRegex(ControlError, 'control_start_retry_unavailable'):
            contract.reviewed_start_attempt(registered, review_id=str(uuid.uuid4()),
                boot_id=registered.start_boot_id, stop_epoch='b' * 64)


class FirstStartRetryControllerTests(unittest.TestCase):
    def test_missing_or_advanced_authority_cannot_be_reinitialized_by_retry_reader(self):
        intent = failed_attempt()
        selected = SimpleNamespace(status=retry.InstallationStatus.PROVISIONING, installation_id=intent.installation_id, service_uid=1000)
        layout = SimpleNamespace(descriptor=SimpleNamespace(descriptor=selected))
        for authority in ({'initialized': False, 'generation': 0}, {'initialized': True, 'generation': 1}):
            with self.subTest(authority=authority), patch.object(provision_worker, 'layout_for_root', return_value=layout), patch.object(provision_worker, 'read_protected_json', return_value=intent.payload()), patch.object(provision_worker.os, 'geteuid', return_value=1000), patch('lifecycle.control.processes.require_service_quiescent'), patch.object(provision_worker, 'initial_authority', return_value=authority), patch.object(provision_worker, 'read_active_execution') as read:
                with self.assertRaisesRegex(ControlError, 'control_operation_recovery_required'):
                    provision_worker.initial_retry_check()
            read.assert_not_called()

    @contextmanager
    def controller(self, intent):
        expected = {'installation_id': intent.installation_id, 'intent_sha256': contract.intent_sha256(intent),
            'generation': 0, 'execution_sha256': intent.execution_sha256}
        selected = SimpleNamespace(status=retry.InstallationStatus.PROVISIONING, installation_id=intent.installation_id)
        defaults = {'require_root': None, 'require_supported_platform': None, '_require_install_scope': None,
            'administrative_lock': nullcontext(), 'read_intent': intent, 'descriptor': selected,
            '_inactive': None, 'run_worker': expected, '_consent': True, 'root_stop_epoch': intent.start_stop_epoch,
            '_persist': None, 'systemctl': '', '_finish_registration': None, 'publish_start_plan': None}
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(retry, name, return_value=value)) for name, value in defaults.items()}
            def registered(candidate, **kwargs):
                consumed = contract.advance_intent(candidate, start_permit='consumed', start_invocation='2' * 32)
                return contract.advance_intent(consumed, stage='registered')
            mocks['start'] = stack.enter_context(patch.object(retry, '_first_start', side_effect=registered))
            mocks['mirror'] = stack.enter_context(patch('lifecycle.control.provisioning_mirror.publish_mirror'))
            yield mocks

    def test_decline_never_creates_permission_or_requests_an_os_start(self):
        intent = failed_attempt()
        with self.controller(intent) as mocks:
            mocks['_consent'].return_value = False
            self.assertEqual(retry.retry_scoped(intent.installation_id, intent.start_stop_epoch,
                manifest={'bundle_id': intent.control_bundle_id}, manifest_sha256=intent.control_manifest_sha256,
                request_path=Path('/request')), 0)
            mocks['_persist'].assert_not_called()
            mocks['systemctl'].assert_not_called()
            mocks['start'].assert_not_called()

    def test_exact_new_permission_is_durable_before_failure_counter_and_start(self):
        intent = failed_attempt()
        events = []
        with self.controller(intent) as mocks:
            mocks['_persist'].side_effect = lambda value: events.append('persist')
            mocks['publish_start_plan'].side_effect = lambda value: events.append('plan')
            mocks['systemctl'].side_effect = lambda *args: events.append(args)
            original = mocks['start'].side_effect
            def start(*args, **kwargs):
                events.append('start')
                return original(*args, **kwargs)
            mocks['start'].side_effect = start
            retry.retry_scoped(intent.installation_id, intent.start_stop_epoch,
                manifest={'bundle_id': intent.control_bundle_id}, manifest_sha256=intent.control_manifest_sha256,
                request_path=Path('/request'))
            self.assertEqual(events, ['persist', 'plan', ('reset-failed', 'streamhome.service'), 'start'])
            candidate = mocks['_persist'].call_args.args[0]
            self.assertEqual(candidate.start_attempts[0].previous_intent_sha256, contract.intent_sha256(intent))
            self.assertEqual(candidate.start_attempts[0].invocation, intent.start_invocation)
            self.assertEqual(candidate.start_permit, 'issued')
            self.assertEqual(mocks['run_worker'].call_count, 2)
            mocks['_finish_registration'].assert_called_once()

    def test_stop_or_changed_artifacts_during_consent_prevents_new_permission(self):
        intent = failed_attempt()
        for scenario in ('stop', 'artifacts'):
            with self.subTest(scenario=scenario), self.controller(intent) as mocks:
                if scenario == 'stop':
                    mocks['root_stop_epoch'].return_value = 'f' * 64
                else:
                    mocks['run_worker'].side_effect = [mocks['run_worker'].return_value, {'changed': True}]
                with self.assertRaises(ControlError):
                    retry.retry_scoped(intent.installation_id, intent.start_stop_epoch,
                        manifest={'bundle_id': intent.control_bundle_id}, manifest_sha256=intent.control_manifest_sha256,
                        request_path=Path('/request'))
                mocks['_persist'].assert_not_called()
                mocks['systemctl'].assert_not_called()
                mocks['start'].assert_not_called()

    def test_os_job_absence_requires_the_qualified_named_property(self):
        inactive = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'MainPID': '0'}
        for job in ('Job=', 'Job=0', '', 'Job=123 /org/freedesktop/systemd1/job/123'):
            with self.subTest(job=job), patch.object(retry, 'unit_snapshot', return_value=inactive), patch.object(retry, 'systemctl', return_value=job), patch('lifecycle.control.stop._require_empty') as empty:
                if job in {'Job=', 'Job=0'}:
                    retry._inactive()
                    empty.assert_called_once_with('streamhome.service')
                else:
                    with self.assertRaisesRegex(ControlError, 'control_stop_before_first_start_retry'):
                        retry._inactive()
                    empty.assert_not_called()

    def test_noninteractive_consent_is_exact_and_cannot_adopt_a_changed_intent(self):
        intent = failed_attempt()
        payload = {'schema_version': 1, 'installation_id': intent.installation_id,
            'expected_intent_sha256': contract.intent_sha256(intent), 'retry_first_start': True, 'start_approved': True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            path.write_text(json.dumps(payload))
            self.assertTrue(retry._consent(path, intent))
            path.write_text(json.dumps({**payload, 'start_approved': False}))
            self.assertFalse(retry._consent(path, intent))
            path.write_text(json.dumps({**payload, 'expected_intent_sha256': 'f' * 64}))
            with self.assertRaisesRegex(ControlError, 'control_review_stale'):
                retry._consent(path, intent)

    def test_root_retry_imports_no_canonical_or_application_implementation(self):
        script = "import sys; import lifecycle.control.first_start_retry; assert not {'lifecycle.state','lifecycle.control.workers','config','models','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
