import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import setup_bootstrap as bootstrap


class SetupBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state, self.runtime = self.root/'state', self.root/'run'
        self.state.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        self.identity = str(uuid.uuid4())
        self.wall, self.monotonic = 1000.0, 100.0
        bootstrap.provision_eligibility(self.state, self.identity)
        self.authority = self.make_authority()

    def make_authority(self):
        return bootstrap.SetupAuthority(
            self.identity, 'a'*32, self.state, self.runtime,
            wall_clock=lambda: self.wall, monotonic_clock=lambda: self.monotonic,
        )

    async def test_startup_never_issues_or_prints_a_code(self):
        self.assertFalse((self.runtime/'bootstrap-code.json').exists())
        self.assertIsNone(self.authority._verifier)
        self.assertFalse(self.authority.finalized)

    async def test_retrieval_keeps_code_expiry_and_private_storage(self):
        first = await self.authority.issue()
        self.wall += 60
        self.monotonic += 60
        second = await self.authority.issue()
        self.assertEqual(second.code, first.code)
        self.assertEqual(second.expires_at, first.expires_at)
        self.assertEqual(second.remaining_seconds, 840)
        self.assertEqual((self.runtime/'bootstrap-code.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(first.code, repr(first))
        self.assertNotIn(first.code, repr(vars(self.authority)))
        self.assertNotIn(first.code, (self.state/'setup-finalization.json').read_text())

    async def test_expiry_and_monotonic_time_prevent_sliding(self):
        first = await self.authority.issue()
        self.monotonic += 901
        self.wall -= 100
        self.assertFalse(await self.authority.consume(first.code))
        replacement = await self.authority.issue()
        self.assertNotEqual(replacement.code, first.code)

    async def test_consumption_is_once_and_bad_attempt_does_not_consume(self):
        issued = await self.authority.issue()
        self.assertFalse(await self.authority.consume('incorrect'))
        results = await asyncio.gather(self.authority.consume(issued.code), self.authority.consume(issued.code))
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse((self.runtime/'bootstrap-code.json').exists())
        replacement = await self.authority.issue()
        self.assertNotEqual(replacement.code, issued.code)

    async def test_renewal_invalidates_previous_code(self):
        first = await self.authority.issue()
        second = await self.authority.issue(renew=True)
        self.assertNotEqual(first.code, second.code)
        self.assertFalse(await self.authority.consume(first.code))
        self.assertTrue(await self.authority.consume(second.code))

    async def test_restart_removes_raw_material_and_invalidates_old_code(self):
        first = await self.authority.issue()
        restarted = self.make_authority()
        self.assertFalse((self.runtime/'bootstrap-code.json').exists())
        self.assertFalse(await restarted.consume(first.code))
        self.assertNotEqual((await restarted.issue()).code, first.code)

    async def test_missing_eligibility_never_initializes_during_startup(self):
        (self.state/'setup-finalization.json').unlink()
        with self.assertRaises(bootstrap.SetupAuthorityError):
            self.make_authority()
        self.assertFalse((self.state/'setup-finalization.json').exists())

    async def test_completion_closes_all_codes_and_provision_retry_cannot_reset_it(self):
        first = await self.authority.issue()
        async with self.authority.lock:
            self.authority.finalize_locked(str(uuid.uuid4()))
        bootstrap.provision_eligibility(self.state, self.identity)
        restarted = self.make_authority()
        self.assertTrue(restarted.finalized)
        self.assertFalse((self.runtime/'bootstrap-code.json').exists())
        for authority in (self.authority, restarted):
            with self.assertRaises(bootstrap.SetupAlreadyFinalized):
                await authority.issue()
            with self.assertRaises(bootstrap.SetupAlreadyFinalized):
                await authority.consume(first.code)

    async def test_older_or_empty_restored_database_does_not_reopen_finalized_installation(self):
        await self.authority.reconcile(has_account=True, database_finalization_id=str(uuid.uuid4()))
        restarted = self.make_authority()
        await restarted.reconcile(has_account=False, database_finalization_id=None)
        self.assertTrue(restarted.finalized)
        with self.assertRaises(bootstrap.SetupAlreadyFinalized):
            await restarted.issue()

    async def test_database_commit_receipt_repairs_lost_file_finalization_reply(self):
        finalization = str(uuid.uuid4())
        await self.authority.reconcile(has_account=False, database_finalization_id=finalization)
        self.assertEqual(bootstrap.read_finalization(self.state, self.identity)['finalization_id'], finalization)

    async def test_missing_receipt_repairs_only_to_finalized_with_database_proof(self):
        (self.state/'setup-finalization.json').unlink()
        with self.assertRaises(bootstrap.SetupAuthorityError):
            bootstrap.recover_committed_finalization(self.state, self.identity, has_account=False, database_finalization_id=None)
        self.assertFalse((self.state/'setup-finalization.json').exists())
        finalization = str(uuid.uuid4())
        bootstrap.recover_committed_finalization(self.state, self.identity, has_account=False, database_finalization_id=finalization)
        recovered = self.make_authority()
        self.assertTrue(recovered.finalized)
        self.assertEqual(bootstrap.read_finalization(self.state, self.identity)['finalization_id'], finalization)
        with self.assertRaises(bootstrap.SetupAlreadyFinalized):
            await recovered.issue()

    async def test_account_proof_never_overwrites_foreign_or_corrupt_receipt(self):
        path = self.state/'setup-finalization.json'
        path.write_text('{}')
        with self.assertRaises(bootstrap.SetupAuthorityError):
            bootstrap.recover_committed_finalization(self.state, self.identity, has_account=True, database_finalization_id=None)
        self.assertEqual(path.read_text(), '{}')

    async def test_durable_failure_after_database_commit_stays_closed_in_process(self):
        async with self.authority.lock:
            with patch.object(bootstrap.state, '_atomic_write_json', side_effect=bootstrap.state.PersistenceError('injected')):
                with self.assertRaises(bootstrap.SetupAuthorityError):
                    self.authority.finalize_locked(str(uuid.uuid4()))
        self.assertTrue(self.authority.finalized)
        with self.assertRaises(bootstrap.SetupAlreadyFinalized):
            await self.authority.issue()

    async def test_foreign_or_unsafe_code_file_is_not_returned(self):
        await self.authority.issue()
        path = self.runtime/'bootstrap-code.json'
        payload = json.loads(path.read_text())
        payload['invocation'] = 'b'*32
        path.write_text(json.dumps(payload))
        with self.assertRaises(bootstrap.SetupAuthorityError):
            await self.authority.issue()
        path.chmod(0o644)
        with self.assertRaises(bootstrap.SetupAuthorityError):
            self.make_authority()


if __name__ == '__main__':
    unittest.main()
