from array import array
from dataclasses import replace
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
import uuid

from lifecycle.control import descriptor_transport as transport
from lifecycle.control.rpc import ControlError


class DescriptorTransportTests(unittest.TestCase):
    def setUp(self):
        self.binding = transport.Binding(str(uuid.uuid4()), str(uuid.uuid4()), 7, 'a' * 32,
                                         str(uuid.uuid4()), 'restore')
        self.sender, self.receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.sender.close);self.addCleanup(self.receiver.close)
        self.pipes = [os.pipe() for _ in range(3)]
        self.addCleanup(lambda: transport.close_descriptors(fd for pair in self.pipes for fd in pair))
        self.descriptors = self.pipes[0][0], self.pipes[1][1], self.pipes[2][0]
        self.peer = {'expected_uid': os.geteuid(), 'expected_pid': os.getpid()}

    def send(self, binding=None, descriptors=None):
        transport.send_offer(self.sender, binding or self.binding,
                             descriptors or self.descriptors, **self.peer)

    def receive(self, binding=None):
        return transport.receive_offer(self.receiver, binding or self.binding, **self.peer)

    def test_actual_fd_transfer_preserves_secret_and_ack_and_separate_response_gate(self):
        self.send();received = self.receive()
        self.addCleanup(transport.close_descriptors, received)
        self.assertTrue(all(not os.get_inheritable(fd) for fd in received))
        os.write(self.pipes[0][1], b'capability-in-pipe-only')
        self.assertEqual(os.read(received[0], 128), b'capability-in-pipe-only')
        os.write(received[1], b'owned-generation-8')
        self.assertEqual(os.read(self.pipes[1][0], 128), b'owned-generation-8')
        os.close(self.pipes[2][0])
        self.pipes[2] = -1, self.pipes[2][1]
        os.write(self.pipes[2][1], b'1')
        self.assertEqual(os.read(received[2], 1), b'1')
        # Neither transfer nor ACK fabricates response completion; EOF remains
        # observably distinct from the explicit one-byte ASGI gate signal.
        os.close(self.pipes[2][1]);self.pipes[2] = -1, -1
        self.assertEqual(os.read(received[2], 1), b'')

    def test_peer_identity_mismatch_is_refused_before_receiving_descriptors(self):
        self.send()
        with self.assertRaises(ControlError):
            transport.receive_offer(self.receiver, self.binding, expected_uid=os.geteuid(), expected_pid=os.getpid()+1)
        received = self.receive();transport.close_descriptors(received)

    def test_distinct_process_receives_only_transferred_channels_and_waits_for_response_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = str(Path(directory) / 'handoff.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(endpoint);listener.listen(1);listener.settimeout(5)
                source = str(Path(transport.__file__).resolve().parents[2])
                script = '''import json,os,socket,sys
sys.path.insert(0,sys.argv[1])
from lifecycle.control.descriptor_transport import parse_binding,receive_offer,close_descriptors
binding=parse_binding(json.loads(sys.argv[3]))
with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as connection:
 connection.settimeout(5);connection.connect(sys.argv[2])
 received=receive_offer(connection,binding,expected_uid=os.geteuid(),expected_pid=int(sys.argv[4]))
 try:
  assert os.read(received[0],64)==b'secret-through-pipe'
  os.write(received[1],b'owned')
  assert os.read(received[2],1)==b'1'
 finally:close_descriptors(received)
'''
                child = subprocess.Popen([sys.executable, '-I', '-c', script, source, endpoint,
                    json.dumps(self.binding.payload()), str(os.getpid())], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
                try:
                    with listener.accept()[0] as connection:
                        connection.settimeout(5)
                        transport.send_offer(connection, self.binding, self.descriptors,
                                             expected_uid=os.geteuid(), expected_pid=child.pid)
                    os.write(self.pipes[0][1], b'secret-through-pipe')
                    self.assertTrue(select.select([self.pipes[1][0]], [], [], 5)[0])
                    self.assertEqual(os.read(self.pipes[1][0], 5), b'owned')
                    self.assertIsNone(child.poll())
                    os.write(self.pipes[2][1], b'1')
                    stdout, stderr = child.communicate(timeout=5)
                    self.assertEqual((child.returncode, stdout, stderr), (0, b'', b''))
                finally:
                    if child.poll() is None:
                        child.terminate();child.communicate(timeout=5)

    def test_changed_binding_closes_every_received_descriptor(self):
        self.send()
        before = set(os.listdir('/proc/self/fd'))
        with self.assertRaisesRegex(ControlError, 'control_handoff_changed'):
            self.receive(replace(self.binding, generation=8))
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_packet_without_descriptors_is_not_a_handoff(self):
        self.sender.send(json.dumps(self.binding.payload()).encode())
        with self.assertRaises(ControlError):self.receive()

    def test_oversize_packet_and_extra_descriptors_are_closed_on_rejection(self):
        extra = os.pipe();self.addCleanup(transport.close_descriptors, extra)
        rights = array('i', (*self.descriptors, extra[0]))
        for raw in (b'x' * (transport.MAX_OFFER+1), json.dumps(self.binding.payload()).encode()):
            before = set(os.listdir('/proc/self/fd'))
            self.sender.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
            with self.assertRaises(ControlError):self.receive()
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_same_pipe_different_ends_and_files_are_not_three_distinct_channels(self):
        with self.assertRaises(ControlError):
            self.send(descriptors=(self.pipes[0][0], self.pipes[0][1], self.pipes[2][0]))
        with tempfile.TemporaryFile() as handle, self.assertRaises(ControlError):
            self.send(descriptors=(handle.fileno(), self.pipes[1][1], self.pipes[2][0]))
        with self.assertRaises(ControlError):
            self.send(descriptors=(self.pipes[0][1], self.pipes[1][1], self.pipes[2][0]))

    def test_strict_binding_rejects_bool_generation_unknown_fields_and_zero_identity(self):
        original = self.binding.payload()
        for changed in ({**original, 'generation': True}, {**original, 'operation_type': 'management'},
                        {**original, 'operation_type': []}, {**original, 'capability': 'forbidden'},
                        {**original, 'operation_id': str(uuid.UUID(int=0))},
                        {**original, 'supervisor_invocation_id': '0' * 32}):
            with self.subTest(changed=changed), self.assertRaises(ControlError):
                transport.parse_binding(changed)

    def test_stream_socket_is_refused_and_early_eof_is_not_response_completion(self):
        left, right = socket.socketpair()
        with left, right, self.assertRaises(ControlError):
            transport.send_offer(left, self.binding, self.descriptors, **self.peer)
        self.sender.close()
        with self.assertRaises(ControlError):self.receive()


if __name__ == '__main__':
    unittest.main()
