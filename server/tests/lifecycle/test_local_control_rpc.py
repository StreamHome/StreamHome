import asyncio
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest
import uuid

from lifecycle.control import rpc


class LocalControlRPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'control.sock'
        self.identity = str(uuid.uuid4())
        self.calls = []
        async def handle(peer, action, arguments):
            self.calls.append((peer, action, arguments))
            return {'acknowledged': True}
        self.server = rpc.Server(self.path, self.identity, 'a'*32, handle, allowed_uids=frozenset({os.getuid()}))
        await self.server.start()
        self.addAsyncCleanup(self.server.close)

    async def request(self, **kwargs):
        return await asyncio.to_thread(rpc.request, self.path, expected_uid=kwargs.pop('expected_uid', os.getuid()), installation_id=kwargs.pop('installation_id', self.identity), invocation=kwargs.pop('invocation', 'a'*32), action='status', arguments={}, **kwargs)

    async def test_kernel_peer_credentials_and_incarnation_are_bound(self):
        self.assertEqual(await self.request(expected_pid=os.getpid()), {'acknowledged': True})
        self.assertEqual(self.calls[0][0].uid, os.getuid())
        self.assertEqual(self.calls[0][0].pid, os.getpid())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(rpc.ControlError):
            await self.request(invocation='b'*32)
        self.assertEqual(len(self.calls), 1)

    async def test_client_checks_server_uid_before_sending_any_arguments(self):
        with self.assertRaises(rpc.ControlError):
            await self.request(expected_uid=os.getuid()+1)
        self.assertEqual(self.calls, [])

    async def test_only_guarded_update_start_accepts_extended_startup_deadline(self):
        for action in ('update_start_candidate', 'update_start_previous'):
            result = await asyncio.to_thread(rpc.request, self.path, expected_uid=os.getuid(),
                installation_id=self.identity, invocation='a'*32, action=action,
                arguments={}, timeout=930)
            self.assertEqual(result, {'acknowledged': True})
        with self.assertRaisesRegex(rpc.ControlError, 'control_timeout_invalid'):
            await self.request(timeout=930)
        with self.assertRaisesRegex(rpc.ControlError, 'control_timeout_invalid'):
            await asyncio.to_thread(rpc.request, self.path, expected_uid=os.getuid(),
                installation_id=self.identity, invocation='a'*32, action='update_start_candidate',
                arguments={}, timeout=931)

    async def test_unknown_caller_is_rejected_before_reading_a_request(self):
        self.server.allowed_uids = frozenset()
        with self.assertRaises(rpc.ControlError):
            await self.request()
        self.assertEqual(self.calls, [])

    async def test_another_installation_cannot_reuse_the_socket(self):
        with self.assertRaises(rpc.ControlError):
            await self.request(installation_id=str(uuid.uuid4()))
        self.assertEqual(self.calls, [])

    async def test_existing_endpoint_is_never_removed_to_force_startup(self):
        other = rpc.Server(self.path, self.identity, 'a'*32, self.server.handler, allowed_uids=frozenset({os.getuid()}))
        identity = self.path.stat().st_ino
        with self.assertRaises(rpc.ControlError):
            await other.start()
        self.assertEqual(self.path.stat().st_ino, identity)
        self.assertEqual(await self.request(), {'acknowledged': True})

    async def test_transport_does_not_echo_internal_failure_or_secret(self):
        async def broken(*args):
            raise ValueError('private-setup-code-must-never-be-echoed')
        self.server.handler = broken
        with self.assertRaises(rpc.ControlError) as result:
            await self.request()
        self.assertEqual(str(result.exception), 'control_unavailable')

    async def test_frame_bounds_duplicate_fields_and_nonstandard_numbers_are_rejected(self):
        for raw in (b'{"value":1,"value":2}', b'{"value":NaN}', b'[]', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(rpc.ControlError):
                rpc.decode(raw)
        with self.assertRaises(rpc.ControlError):
            rpc.encode({'value': 'x'*(rpc.MAX_MESSAGE+1)})
        with self.assertRaises(rpc.ControlError):
            rpc.validate_request({'action': 'shell', 'arguments': {'command': 'arbitrary'}})

    async def test_oversized_frame_is_closed_without_handler_execution(self):
        def malformed():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(str(self.path))
                connection.sendall(struct.pack('!I', rpc.MAX_MESSAGE+1))
                return connection.recv(4096)
        result = await asyncio.to_thread(malformed)
        self.assertNotIn(b'private', result)
        self.assertEqual(self.calls, [])

    async def test_shutdown_does_not_delete_a_replaced_unrelated_file(self):
        self.path.unlink()
        self.path.write_text('preserve this unrelated file')
        await self.server.close()
        self.assertEqual(self.path.read_text(), 'preserve this unrelated file')


if __name__ == '__main__':
    unittest.main()
