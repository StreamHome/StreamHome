"""Finite root capsules preserve each original owner's permission unchanged."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import control_parent_requests as requests,parent_grants
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_bound_control_parent_controller as fixtures


class ParentControlRequestTests(unittest.TestCase):
    def fixture(self,kind):
        self.read_parent=parent_grants.read_grant
        case=fixtures.ParentControlControllerTests();case.setUp();case.setup_owner(kind)
        self.addCleanup(case.doCleanups);self.case=case
        return requests.wrap(case.grant)

    def test_capsule_preserves_original_update_permission_and_has_no_start_plan(self):
        capsule=self.fixture('update')
        self.assertEqual(requests.parse(capsule)['owner_grant'],self.case.grant)
        self.assertEqual(capsule['request']['expected_generation'],self.case.grant['generation'])
        self.assertEqual(capsule['source_request_sha256'],self.case.grant['parent_binding_sha256'])
        self.assertNotIn('resumption_plan_id',capsule)

    def test_restore_native_reader_checks_capsule_and_exact_retained_review_before_unwrapping(self):
        capsule=self.fixture('restore');operation=self.case.operation;generation=self.case.grant['generation']+1
        retained=deepcopy(capsule)
        def read(path):
            return retained if path.name.startswith('recovery-review-') else capsule
        with patch.object(parent_grants,'read_protected_json',side_effect=read):
            self.assertEqual(self.read_parent(operation,generation),self.case.grant)
            retained['filesystem_sha256']='f'*64
            with self.assertRaisesRegex(ControlError,'control_parent_recovery_request_changed'):
                self.read_parent(operation,generation)

    def test_capsule_cannot_change_native_owner_identity_artifact_expiry_scope_or_schema(self):
        capsule=self.fixture('update')
        changes=({'schema_version':True},{'schema_version':5},{'unknown':True},
            {'request':{**capsule['request'],'operation_id':str(uuid.uuid4())}},
            {'request':{**capsule['request'],'expected_generation':True}},
            {'artifact':{**capsule['artifact'],'artifact_sha256':'f'*64}},
            {'selection':{**capsule['selection'],'schema_version':True}},
            {'expires_at_monotonic':capsule['expires_at_monotonic']+1},
            {'issuer_scope':{**capsule['issuer_scope'],'pid':9999}},
            {'owner_grant':{**capsule['owner_grant'],'action':'continue_recovery'}},
            {'owner_grant':{**capsule['owner_grant'],'operation_type':'restore'}},
            {'owner_grant':{**capsule['owner_grant'],'schema_version':3}})
        for change in changes:
            with self.subTest(change=change),self.assertRaises(ControlError):requests.parse({**capsule,**change})

    def test_metadata_imports_no_canonical_owner_or_application_modules(self):
        code="import sys;import lifecycle.control.control_parent_requests;assert not {'lifecycle.state','lifecycle.restore_controller','lifecycle.update_controller','config','database','application','fastapi','sqlmodel'}.intersection(sys.modules)"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
