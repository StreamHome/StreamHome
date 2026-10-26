"""Real repeated canonical transfer gaps retain exact root-grant journal proof."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from lifecycle import bound_parent_repair, parent_repair_intents as intents, restore_controller as restore, state
from lifecycle.control import parent_grants
from lifecycle.control.resumption_contract import digest
from lifecycle.control.rpc import ControlError
from tests.lifecycle import test_bound_parent_repair as fixtures


class ParentRepairIntentTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.BoundParentRepairPlanTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.root=f.root;self.operation=f.operation
        current=f.plan()
        scope={'unit':'streamhome-install-'+f.bound.installation_id+'-'+uuid.uuid4().hex+'.scope',
            'invocation':'a'*32,'pid':4321,'start_identity':'123'}
        self.scope=self.enterContext(patch.object(parent_grants,'require_scope'))
        self.enterContext(patch.object(parent_grants,'read_stop_epoch',return_value='b'*64))
        self.grant=bound_parent_repair.prepare(self.root,{'operation_id':self.operation,
            'expected_evidence_sha256':current['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
            'issuer_scope':scope,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'stop_epoch':'b'*64},control_manifest_sha256=f.bound.execution.control_manifest_sha256)
        self.records={3:self.grant}
        self.reader=self.enterContext(patch.object(parent_grants,'read_grant',side_effect=lambda op,generation,**kw:self.records[generation]))

    def transfer_without_journal(self,grant):
        source=state.inspect_state(self.root)
        journal=restore.inspect_restore_journal(self.root,self.operation)
        capability=state.load_controller_capability(self.root,operation_id=self.operation,generation=source.generation)
        lease,result=state.acquire_transferred_controller_lease(self.root,operation_id=self.operation,
            generation=source.generation,expected_phase='recovery_required',owner_capability=capability,
            next_owner_kind='restore_controller',
            pre_transfer_check=lambda:intents.prepare_transfer(self.root,grant,journal))
        lease.release()  # Simulate controller death before the normal journal CAS.
        return result.state,restore.inspect_restore_journal(self.root,self.operation)

    def test_exact_root_permission_and_intent_prove_actual_transfer_before_journal_gap(self):
        authority,journal=self.transfer_without_journal(self.grant)
        self.assertEqual((authority.generation,journal['generation']),(3,2))
        before=self.fixture.snapshot();witness=intents.gap_witness(self.root,authority,journal)
        self.assertEqual(witness['grant_sha256'],digest(self.grant))
        self.assertEqual(witness['controller_generation'],3)
        self.assertEqual(self.fixture.snapshot(),before)
        self.assertEqual(self.reader.call_args.kwargs,{'live':False})

    def repeated_gaps(self):
        original=restore.inspect_restore_journal(self.root,self.operation)
        database=self.fixture.database.read_bytes();grant=self.grant
        for target in (3,4,5,6):
            authority,journal=self.transfer_without_journal(grant)
            self.assertEqual((authority.generation,journal['generation']),(target,target-1))
            witness=intents.gap_witness(self.root,authority,journal)
            self.assertEqual(witness['controller_generation'],target)
            self.assertTrue(intents.same_parent_journal(original,journal,target-1))
            self.assertEqual(self.fixture.database.read_bytes(),database)
            # These are synthetic root permissions for a separately fresh review;
            # this test exercises intent/generation mechanics, not plan issuance.
            grant={**grant,'generation':target,'review_id':str(uuid.uuid4()),
                'source_journal_generation':journal['generation'],'source_journal_sha256':digest(journal),
                'source_evidence_sha256':digest({'separate_review':target})}
            self.records[target+1]=parent_grants.parse_grant(grant)

    def test_repeated_interrupted_fresh_repairs_normalize_only_metadata_before_next_transfer(self):
        self.repeated_gaps()

    def test_accepted_parent_repeated_gaps_preserve_actual_database_acceptance(self):
        self.fixture.accept_parent()
        reviewed=self.fixture.plan()
        self.grant={**self.grant,'source_journal_sha256':reviewed['source_journal_sha256'],
            'source_evidence_sha256':reviewed['evidence_sha256']}
        self.records[3]=self.grant
        self.repeated_gaps()

    def test_readonly_gap_plan_and_fresh_permission_do_not_require_old_issuer_liveness(self):
        authority,journal=self.transfer_without_journal(self.grant)
        before=self.fixture.snapshot()
        with patch.object(parent_grants.time,'monotonic',return_value=self.grant['expires_at_monotonic']+1):
            with self.assertRaises(ControlError):parent_grants.require_live(self.grant)
            reviewed=self.fixture.plan()
            self.assertEqual(reviewed['generation'],3)
            self.assertEqual(reviewed['source_journal_generation'],2)
            self.assertEqual(reviewed['generation_gap']['controller_generation'],3)
            fresh=bound_parent_repair.prepare(self.root,{'operation_id':self.operation,
                'expected_evidence_sha256':reviewed['evidence_sha256'],'recovery_review_id':str(uuid.uuid4()),
                'issuer_scope':self.grant['issuer_scope'],'boot_id':self.grant['boot_id'],'stop_epoch':'b'*64},
                control_manifest_sha256=self.fixture.bound.execution.control_manifest_sha256)
            parent_grants.require_live(fresh)
        self.assertEqual(fresh['generation'],3);self.assertEqual(fresh['source_journal_generation'],2)
        self.assertEqual(self.fixture.snapshot(),before)
        self.assertEqual(state.inspect_state(self.root),authority)

    def test_changed_bound_journal_or_different_root_permission_cannot_explain_gap(self):
        authority,journal=self.transfer_without_journal(self.grant)
        path=intents.path(self.root,self.grant)
        before=intents.read(self.root,self.grant)
        corrupt=deepcopy(before);corrupt['source_journal']['candidate_sha256']='f'*64
        state._atomic_write_json(path,corrupt)
        with self.assertRaises(ControlError):intents.gap_witness(self.root,authority,journal)
        state._atomic_write_json(path,before)
        self.records[3]={**self.grant,'review_id':str(uuid.uuid4())}
        with self.assertRaises((ControlError,OSError)):intents.gap_witness(self.root,authority,journal)

    def test_revoked_fresh_permission_never_normalizes_or_transfers_another_generation(self):
        authority,journal=self.transfer_without_journal(self.grant)
        fresh={**self.grant,'generation':3,'review_id':str(uuid.uuid4())}
        before=self.fixture.snapshot();self.scope.side_effect=ControlError('control_operator_stopped')
        with self.assertRaises(ControlError):intents.prepare_transfer(self.root,fresh,journal)
        self.assertEqual(state.inspect_state(self.root),authority)
        self.assertEqual(self.fixture.snapshot(),before)


if __name__=='__main__':unittest.main()
