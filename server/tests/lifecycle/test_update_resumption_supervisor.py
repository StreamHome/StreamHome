"""Enrolled supervisor uses exact UPDATE tuple and never duplicates its first launch."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from lifecycle import managed_update
from lifecycle.control import supervisor
from lifecycle.control.rpc import ControlError, Peer
from tests.lifecycle import test_update_resumption_runtime as runtime_fixture


class UpdateResumptionSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = runtime_fixture.UpdateResumptionRuntimeTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.fixture = fixture
        self.binding = fixture.binding()
        self.enterContext(patch.object(supervisor, 'ROOT', fixture.root))
        obj = supervisor.Supervisor.__new__(supervisor.Supervisor)
        obj.launches_open = True;obj.installation = self.binding.installation_id
        obj.invocation = self.binding.invocation;obj.resumption_binding = self.binding
        obj.execution = fixture.fixture.fixture.bound.accepted
        obj.children = {'backend': object(), 'web': object()};obj.lock = asyncio.Lock()
        obj.launch = AsyncMock();obj.wait_update_guarded = AsyncMock();self.sup = obj

    def journal(self):
        with patch.object(managed_update.os, 'getpid', return_value=self.fixture.main):
            return self.sup._update_journal(self.fixture.operation, 3)

    async def test_new_supervisor_proves_current_ram_enrollment_without_relabeling_original_parent(self):
        before = self.fixture.canonical.path.read_bytes()
        authority, journal, bound = self.journal()
        self.assertEqual(authority.generation, 3)
        self.assertNotEqual(self.sup.invocation, bound.parent.supervisor_invocation_id)
        self.assertEqual(self.fixture.canonical.path.read_bytes(), before)
        self.sup.invocation = bound.parent.supervisor_invocation_id
        with self.assertRaises(ControlError):self.journal()

    async def test_controller_start_acknowledges_only_exact_already_enrolled_pair_without_second_launch(self):
        update = self.journal()
        self.sup._update_controller = Mock(return_value=update)
        arguments = {'operation_id': self.fixture.operation, 'generation': 3}
        peer = Peer(self.fixture.main, os.geteuid(), os.getegid())
        with patch.object(managed_update.os, 'getpid', return_value=self.fixture.main):
            result = await self.sup.handle(peer, 'update_start_previous', arguments)
        self.assertEqual(result, {'launched': True, 'build_id': self.sup.execution.application_build_id})
        self.sup.launch.assert_not_awaited()
        self.sup.wait_update_guarded.assert_awaited_once_with(peer, arguments)
        self.sup.execution = update[2].candidate
        with patch.object(managed_update.os, 'getpid', return_value=self.fixture.main), self.assertRaises(ControlError):
            await self.sup.handle(peer, 'update_start_previous', arguments)
        self.sup.launch.assert_not_awaited()
