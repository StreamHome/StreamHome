from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import patch
import uuid

from lifecycle import port_check
from lifecycle.installation import InstallationDescriptor, InstallationEvidenceError, InstallationStatus


class WebPortCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]

    def test_foreign_listener_is_a_conflict_even_when_port_matches_settings(self) -> None:
        with self.assertRaises(port_check.ListenerCheckError):
            port_check.check_web_port("127.0.0.1", self.port)
        with self.assertRaises(port_check.ListenerCheckError):
            port_check.check_web_port("0.0.0.0", self.port)
        self.assertEqual(self.listener.getsockname()[1], self.port)

    def test_unoccupied_port_can_be_probed_without_retaining_socket(self) -> None:
        self.listener.close()
        result = port_check.check_web_port("127.0.0.1", self.port)
        self.assertFalse(result.reusable_owned_listener)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as next_listener:
            next_listener.bind(("127.0.0.1", self.port))

    def test_timewait_is_reported_without_making_a_reuse_probe(self) -> None:
        self.listener.close()
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', self.port))
            listener.listen(1)
            with socket.socket() as client:
                client.connect(listener.getsockname())
                accepted, _ = listener.accept()
                with accepted:
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b'')
        self.assertFalse(port_check._socket_inodes('127.0.0.1', self.port))
        self.assertIn(('06', '0'), port_check._socket_entries('127.0.0.1', self.port))
        with self.assertRaises(port_check.ClosingConnectionsError):
            port_check.check_web_port('127.0.0.1', self.port)

    def test_bound_nonlistening_socket_with_reuse_is_still_a_conflict(self) -> None:
        self.listener.close()
        with socket.socket() as reserved:
            reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reserved.bind(('127.0.0.1', self.port))
            with self.assertRaises(port_check.ListenerCheckError) as raised:
                port_check.check_web_port('127.0.0.1', self.port)
            self.assertNotIsInstance(raised.exception, port_check.ClosingConnectionsError)
            reserved.listen(1)

    def test_actual_proc_socket_inodes_match_current_listener(self) -> None:
        inodes = port_check._socket_inodes("127.0.0.1", self.port)
        self.assertTrue(inodes)
        identity = port_check.OwnedWebListener(str(uuid.uuid4()), os.getuid(), "a" * 32, os.getpid(), "1", "127.0.0.1", self.port)
        self.assertTrue(port_check._owns_all_sockets(identity, inodes))

    def test_process_or_invocation_mismatch_never_reuses_occupied_port(self) -> None:
        descriptor = InstallationDescriptor(str(uuid.uuid4()), InstallationStatus.READY, 12345, 12345)
        owner = port_check.OwnedWebListener(descriptor.installation_id, 12345, "a" * 32, os.getpid(), "1", "127.0.0.1", self.port)
        with self.assertRaises(port_check.ListenerCheckError):
            port_check.check_web_port("127.0.0.1", self.port, owner=owner, installation=descriptor)
        with patch.object(port_check, "_process_matches", side_effect=[True, False]), self.assertRaises(port_check.ListenerCheckError):
            port_check.check_web_port("127.0.0.1", self.port, owner=owner, installation=descriptor)

    def test_reuse_requires_exact_process_and_all_overlapping_sockets(self) -> None:
        descriptor = InstallationDescriptor(str(uuid.uuid4()), InstallationStatus.READY, 12345, 12345)
        owner = port_check.OwnedWebListener(descriptor.installation_id, 12345, "a" * 32, os.getpid(), "1", "127.0.0.1", self.port)
        with patch.object(port_check, "_process_matches", return_value=True):
            self.assertTrue(port_check.check_web_port("127.0.0.1", self.port, owner=owner, installation=descriptor).reusable_owned_listener)
            # A reviewed cutover may replace the same exact owned loopback
            # listener with a LAN binding; it still cannot reuse a foreign one.
            self.assertTrue(port_check.check_web_port("0.0.0.0", self.port, owner=owner, installation=descriptor).reusable_owned_listener)
            with patch.object(port_check, "_socket_inodes", side_effect=[{"1"}, {"2"}]), patch.object(port_check, "_owns_all_sockets", return_value=True), self.assertRaises(port_check.ListenerCheckError):
                port_check.check_web_port("127.0.0.1", self.port, owner=owner, installation=descriptor)

    @unittest.skipUnless(socket.has_ipv6, "IPv6 is unavailable")
    def test_dual_stack_wildcard_blocks_ipv4_reuse(self) -> None:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind(("::", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaises(port_check.ListenerCheckError):
                port_check.check_web_port("127.0.0.1", port)
            self.assertTrue(port_check._socket_inodes("127.0.0.1", port))

    def test_malformed_or_reserved_port_and_binding_are_rejected(self) -> None:
        for value in ("3000oops", "٣٠٠٠", 443, 8000):
            with self.subTest(value=value), self.assertRaises(InstallationEvidenceError):
                port_check.check_web_port("127.0.0.1", value)
        with self.assertRaises(port_check.ListenerCheckError):
            port_check.check_web_port("::", self.port)


if __name__ == "__main__":
    unittest.main()
