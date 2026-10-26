from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import parent_resumption_review as review,resumption_review as shared
from lifecycle.control.rpc import ControlError


class ParentResumptionReviewTests(unittest.TestCase):
    def setUp(self):
        self.snapshot={'operation_type':'restore','runtime_status':'offline','generation':7,
            'installation_id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
            'control_manifest_sha256':'a'*64,'execution_sha256':'b'*64,
            'parent_binding_sha256':'c'*64,'evidence_sha256':'d'*64,'stop_epoch':'e'*64,
            'permitted_actions':['continue_recovery']}
        self.enterContext(patch.object(shared,'require_root'))
        self.enterContext(patch.object(shared,'require_stopped'))
        self.epoch=self.enterContext(patch.object(shared,'root_stop_epoch',return_value='e'*64))
        self.enterContext(patch.object(shared,'current_issuer',return_value={'synthetic_root_scope':'live'}))
        self.boot=self.enterContext(patch.object(shared,'_boot_enabled',return_value=True))
        self.enterContext(redirect_stdout(StringIO()))

    def make(self):return review.make_review(self.snapshot,'continue_recovery',control_manifest_sha256='a'*64)

    def test_fresh_review_binds_parent_and_canonical_plan_without_changing_boot_policy(self):
        value=self.make();plan=review.launch_plan(value,approved=True)
        self.assertEqual(plan['schema_version'],2)
        self.assertEqual(plan['parent_binding_sha256'],self.snapshot['parent_binding_sha256'])
        self.assertEqual(plan['generation'],8);self.assertEqual(len(plan['stages']),1)
        self.assertEqual(plan['stages'][0]['execution_sha256'],self.snapshot['execution_sha256'])
        self.assertEqual(value.payload['boot_enabled'],value.payload['previous_boot_enabled'])

    def test_decline_stop_or_changed_boot_policy_cannot_produce_launch_permission(self):
        value=self.make()
        with self.assertRaises(ControlError):review.launch_plan(value,approved=False)
        self.epoch.return_value='f'*64
        with self.assertRaises(ControlError):review.launch_plan(value,approved=True)
        self.epoch.return_value='e'*64;self.boot.return_value=False
        with self.assertRaises(ControlError):review.launch_plan(value,approved=True)

    def test_old_request_cannot_approve_start_and_exact_fresh_challenge_can(self):
        value=self.make()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'request.json'
            request={'schema_version':2,'operation_id':self.snapshot['operation_id'],'expected_generation':7,
                'expected_evidence_sha256':self.snapshot['evidence_sha256'],'action':'continue_recovery',
                'continue_recovery':True,'interruption_approved':True}
            path.write_text(json.dumps(request))
            with self.assertRaises(ControlError):shared.consent(value,path)
            request.update(schema_version=3,resume_service=True,expected_resumption_sha256=value.sha256)
            path.write_text(json.dumps(request));self.assertTrue(shared.consent(value,path))
            request['expected_resumption_sha256']='f'*64;path.write_text(json.dumps(request))
            with self.assertRaises(ControlError):shared.consent(value,path)

    def test_update_review_reuses_single_consumable_stage_and_cannot_authorize_abort_or_rollback(self):
        self.snapshot['operation_type'] = 'update'
        value = self.make();plan = review.launch_plan(value, approved=True)
        self.assertEqual(plan['owner_type'], 'update')
        self.assertEqual(plan['action'], 'update_recovery')
        self.assertEqual(plan['stages'], [{'kind': 'candidate_start',
            'execution_sha256': self.snapshot['execution_sha256'], 'generation': 8, 'handoff_sha256': None}])
        for action in ('safe_abort', 'rollback'):
            self.snapshot['permitted_actions'] = [action]
            with self.subTest(action=action), self.assertRaises(ControlError):
                review.make_review(self.snapshot, action, control_manifest_sha256='a'*64)


if __name__=='__main__':unittest.main()
