"""Control-only review uses real private configuration and canonical readers."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle import execution_state, management_controller, management_journal as contract, state
from lifecycle.control import workers
from lifecycle.control.requests import parse_management_request
from tests.lifecycle import test_configuration_store as configuration_fixture
from tests.lifecycle.test_management_requests import request_payload


class ControlMaintenanceReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=configuration_fixture.ConfigurationStoreTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root
        selected=Path(self.root/'execution');selected.mkdir(mode=0o700)
        self.layout=SimpleNamespace(state=selected,domains=self.fixture.directory,managed=True,
            descriptor=SimpleNamespace(descriptor=SimpleNamespace(installation_id=self.fixture.identity)))
        self.enterContext(patch.object(execution_state,'layout_for_root',return_value=self.layout))
        self.enterContext(patch.object(workers,'layout_for_root',return_value=self.layout))
        self.enterContext(patch.object(workers,'ROOT',self.root))
        self.payload=request_payload()
        previous=self.payload['previous']
        previous['deployment_revision']=self.fixture.initial.revision
        previous['policy_revisions']={};previous['provider_revisions']={}
        self.previous=contract.parse_execution_tuple(previous)
        record=execution_state.ExecutionRecord(self.fixture.identity,self.previous,str(uuid.uuid4()),0)
        state._atomic_write_json(selected/'active-runtime.json',record.payload())
        self.payload.pop('schema_version');self.payload.pop('installation_id')
        for key in ('action','activation_mode','previous','candidate','expected_desired','controller_control_manifest_sha256'):
            self.payload.pop(key)
        self.payload.update(purpose='maintain_control',expected_generation=0,
            expected_desired_revision=self.fixture.initial.revision,activate_deployment=True,
            boot_enabled=False,previous_boot_enabled=False,
            control_candidate={'control_bundle_id':str(uuid.uuid4()),'control_manifest_sha256':'e'*64,'control_protocol_version':1})

    def prepare(self,payload=None,**kwargs):
        return workers.prepare_management_request(self.payload if payload is None else payload,
            control_manifest_sha256=kwargs.get('digest',self.previous.control_manifest_sha256))

    def test_review_changes_only_control_and_never_claims_or_changes_configuration(self):
        before=self.fixture.paths.state_path.read_bytes()
        config=(self.fixture.directory/'manifest.json').read_bytes()
        reviewed=parse_management_request(self.prepare()['request'])
        self.assertEqual(reviewed.action,contract.ManagementAction.PROVISION_CONTROL)
        self.assertEqual(reviewed.activation_mode,contract.ActivationMode.CUTOVER)
        self.assertEqual(reviewed.previous,self.previous)
        self.assertEqual(reviewed.candidate,replace(self.previous,**self.payload['control_candidate']))
        self.assertEqual(self.fixture.paths.state_path.read_bytes(),before)
        self.assertEqual((self.fixture.directory/'manifest.json').read_bytes(),config)

    def test_stopped_review_preserves_stopped_outcome_and_explicit_consent(self):
        self.payload.update(initial_service_state='stopped',supervisor=None,backend=None,supervisor_invocation_id=None)
        reviewed=parse_management_request(self.prepare()['request'])
        self.assertEqual(reviewed.initial_service_state,'stopped')
        self.assertIsNone(reviewed.supervisor)
        self.payload['interruption_approved']=False
        with self.assertRaises(RuntimeError):self.prepare()

    def test_review_cannot_smuggle_application_configuration_or_boot_changes(self):
        for change in ({'boot_enabled':True},{'activate_deployment':False},
                {'interruption_approved':False},{'expected_generation':True},
                {'expected_desired_revision':str(uuid.uuid4())}):
            with self.subTest(change=change),self.assertRaises((RuntimeError,ValueError)):
                self.prepare({**self.payload,**change})
        for control in ({**self.payload['control_candidate'],'application_commit':'f'*40},
                {**self.payload['control_candidate'],'control_protocol_version':True}):
            with self.subTest(control=control),self.assertRaises((RuntimeError,ValueError)):
                self.prepare({**self.payload,'control_candidate':control})
        changed=deepcopy(self.payload);changed['deployment']['web_port']=4500
        with self.assertRaises((RuntimeError,ValueError)):self.prepare(changed)
        with self.assertRaises(RuntimeError):self.prepare(digest='f'*64)

    def test_competing_operation_refuses_review_without_claiming_again(self):
        state.claim_operation_with_credential(self.root,operation_type='management',owner_kind='management_controller')
        before=self.fixture.paths.state_path.read_bytes()
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.fixture.paths.state_path.read_bytes(),before)

    def test_exact_current_control_is_a_read_only_no_change(self):
        self.payload['control_candidate']={name:getattr(self.previous,name) for name in self.payload['control_candidate']}
        self.assertEqual(self.prepare(),{'no_changes':True})

    def test_unqualified_cutover_cannot_claim_or_use_the_old_supervisor_to_accept_new_control(self):
        reviewed=parse_management_request(self.prepare()['request'])
        controller=management_controller.ManagementController.__new__(management_controller.ManagementController)
        controller.review=reviewed;controller.checkpoint=Mock()
        from lifecycle.control.rpc import ControlError
        with patch.object(state,'claim_management_controller') as claim,patch(
                'lifecycle.control.reader_support.require_control_maintenance_reader',side_effect=ControlError('control_bound_reader_incompatible')):
            with self.assertRaisesRegex(RuntimeError,'control_bound_reader_incompatible'):controller.execute()
            claim.assert_not_called()
        self.assertIsNone(state.inspect_state(self.root).active_operation)
