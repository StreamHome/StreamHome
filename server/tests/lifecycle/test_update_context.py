"""Read canonical UPDATE paths and immutable config without .env assumptions."""
from dataclasses import replace
import json
import os
import unittest

from lifecycle import configuration_store, managed_update, state, update_controller
from lifecycle.control.rpc import ControlError
from lifecycle.deployment_config import default_deployment, patch_deployment
from lifecycle.management_journal import execution_tuple_payload, execution_tuple_sha256
from tests.lifecycle.test_update_delegation import UpdateDelegateFixture


class UpdateContextTests(UpdateDelegateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.layout.domains.mkdir(mode=0o700)
        descriptor = os.open(self.layout.domains, os.O_RDONLY | os.O_DIRECTORY)
        self.store = configuration_store.ConfigurationStore(descriptor, self.parent.installation_id)
        os.close(descriptor);self.addCleanup(self.store.close)
        self.revision = configuration_store.DeploymentRevision(self.parent.installation_id,
            self.parent.execution.deployment_revision, patch_deployment(default_deployment(), {'web_port': 3002}))
        self.store.prepare_revision(self.revision)
        self.manifest = configuration_store.ConfigurationManifest(self.parent.installation_id,
            self.revision.reference, self.revision.reference, (), self.parent.execution.policy_revisions,
            self.parent.execution.provider_revisions)
        self.write_manifest(self.manifest)
        payload = update_controller.read_update_journal(self.path)
        self.candidate = managed_update.prepared_execution(self.parent.execution, payload['target_commit'], self.built)
        payload['managed_candidate'] = execution_tuple_payload(self.candidate)
        self.path.write_text(json.dumps(payload))

    def write_manifest(self, value):
        path = self.layout.domains/'manifest.json'
        path.write_bytes(configuration_store._encode(value.payload()));path.chmod(0o600)

    def test_context_uses_pinned_active_revision_canonical_database_and_bound_interpreters(self):
        # A conflicting legacy .env is deliberately irrelevant.
        (self.root/'.env').write_text('WEB_PORT=9999\nDATABASE_URL=untrusted\n')
        import uuid
        pending = configuration_store.DeploymentRevision(self.parent.installation_id, str(uuid.uuid4()),
            patch_deployment(self.revision.settings, {'web_port': 4001}))
        self.store.prepare_revision(pending)
        self.write_manifest(replace(self.manifest, desired=pending.reference))
        before = self.path.read_bytes(), state.inspect_state(self.root)
        result = managed_update.execution_context(self.root, self.proof())
        self.assertEqual(result['web_port'], 3002)
        self.assertEqual(result['database'], str(self.layout.database))
        self.assertEqual(result['runtime_directory'], str(self.layout.durable_runtime))
        self.assertEqual(result['staging_directory'], str(self.source.parent))
        self.assertEqual(result['previous_python'], self.parent.execution.python_executable)
        self.assertEqual(result['candidate_python'], self.candidate.python_executable)
        self.assertEqual(result['recovery_commit'], self.candidate.application_commit)
        self.assertEqual(result['recovery_target'], 'candidate')
        self.assertEqual(before, (self.path.read_bytes(), state.inspect_state(self.root)))

    def test_accepted_previous_context_retains_original_target_and_needs_no_preparation_checkout(self):
        payload = update_controller.read_update_journal(self.path)
        for old, new in (('reserved', 'exclusive'), ('exclusive', 'verifying')):
            state.transition_operation_with_credential(self.root, operation_id=self.operation, generation=2,
                expected_phase=old, next_phase=new)
        selected = self.parent.execution
        payload.update(lifecycle_phase='verifying', phase='recovering', runtime_committed=True,
            runtime_committed_at=1, current_commit=selected.application_commit,
            managed_verification_execution_sha256=execution_tuple_sha256(selected),
            managed_accepted_execution_sha256=execution_tuple_sha256(selected))
        self.path.write_text(json.dumps(payload));before = self.path.read_bytes()
        from unittest.mock import patch
        with patch.object(managed_update, 'preparation_source', side_effect=AssertionError('Retired source is irrelevant')):
            result = managed_update.execution_context(self.root, self.proof())
        self.assertEqual(result['recovery_commit'], selected.application_commit)
        self.assertEqual(result['recovery_target'], 'previous')
        self.assertEqual(self.path.read_bytes(), before)

    def test_quarantined_or_unrelated_domain_revisions_cannot_change_update_configuration(self):
        for changed in (replace(self.manifest, quarantined=(self.revision.revision,)),
                replace(self.manifest, provider_revisions=()), replace(self.manifest, policy_revisions=())):
            if changed == self.manifest:continue
            self.write_manifest(changed)
            expected = configuration_store.ConfigurationStoreError if changed.quarantined else ControlError
            with self.subTest(changed=changed), self.assertRaises(expected):
                managed_update.execution_context(self.root, self.proof())

    def test_unprepared_candidate_cannot_select_guessed_paths(self):
        payload = update_controller.read_update_journal(self.path)
        payload['managed_candidate'] = None;self.path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ControlError, 'control_update_candidate_unprepared'):
            managed_update.execution_context(self.root, self.proof())


    def test_independent_worker_pins_original_reader_and_rejects_stale_or_extra_requests(self):
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch
        import io
        from lifecycle.control import workers
        request = {'schema_version': 1, 'generation': 2}
        def invoke(payload, digest):
            output = io.StringIO()
            with patch.object(workers, 'ROOT', self.root), patch.object(workers, 'inspect_installation',
                    return_value=self.layout.descriptor), patch.object(workers.sys, 'stdin',
                    SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), redirect_stdout(output):
                result = workers.main(['update-context', self.operation], bundle=self.root,
                    manifest={}, manifest_sha256=digest)
            return result, json.loads(output.getvalue())
        digest = self.parent.execution.control_manifest_sha256
        code, context = invoke(request, digest)
        self.assertEqual(code, 0)
        self.assertEqual(context['web_port'], 3002)
        original = self.path.read_bytes(), state.inspect_state(self.root)
        for payload, reader in ((request, 'f'*64), ({**request, 'generation': 3}, digest),
                ({**request, 'generation': True}, digest), ({**request, 'database': 'other'}, digest)):
            with self.subTest(payload=payload, reader=reader), self.assertRaises(ControlError):invoke(payload, reader)
        self.assertEqual(original, (self.path.read_bytes(), state.inspect_state(self.root)))


    def test_script_preflight_checks_exact_prepared_source_without_rebuilding_or_claiming(self):
        original = self.path.read_bytes(), state.inspect_state(self.root)
        self.assertEqual(managed_update.runtime_action(self.root, self.proof(), 'prepared'), {'prepared': True})
        self.verify.assert_called_once_with(self.root, self.candidate, source_root=self.source)
        self.assertEqual(original, (self.path.read_bytes(), state.inspect_state(self.root)))
        self.verify.side_effect = RuntimeError('Prepared runtime verification failed')
        with self.assertRaises(RuntimeError):managed_update.runtime_action(self.root, self.proof(), 'prepared')
        self.assertEqual(original, (self.path.read_bytes(), state.inspect_state(self.root)))
