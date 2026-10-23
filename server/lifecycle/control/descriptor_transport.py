"""Atomic, bounded transfer of parent-controller pipes over local seqpacket IPC.

This transport conveys existing capability/ACK/response-gate channels. It grants
no lifecycle ownership and cannot start a unit. Callers must additionally prove
the sender's backend/controller cgroup and the exact canonical operation.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import fcntl
import json
import os
import re
import socket
import stat
import uuid

from .rpc import ControlError, decode, peer_credentials


MAX_OFFER = 4096
DESCRIPTORS = 3


@dataclass(frozen=True)
class Binding:
    installation_id: str
    operation_id: str
    generation: int
    supervisor_invocation_id: str
    request_id: str
    operation_type: str

    def payload(self) -> dict:
        value = {'schema_version': 1, **self.__dict__}
        parse_binding(value)
        return value


def parse_binding(value: dict) -> Binding:
    fields = {'schema_version', 'installation_id', 'operation_id', 'generation',
              'supervisor_invocation_id', 'request_id', 'operation_type'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['generation']) is not int or not 0 < value['generation'] < 2 ** 63
            or not isinstance(value['operation_type'], str) or value['operation_type'] not in {'update', 'restore'}
            or not isinstance(value['supervisor_invocation_id'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['supervisor_invocation_id'])
            or int(value['supervisor_invocation_id'], 16) == 0):
        raise ControlError('control_handoff_invalid')
    for name in ('installation_id', 'operation_id', 'request_id'):
        try:
            identity = uuid.UUID(value[name]) if isinstance(value[name], str) else None
        except ValueError as exc:
            raise ControlError('control_handoff_invalid') from exc
        if identity is None or not identity.int or str(identity) != value[name]:
            raise ControlError('control_handoff_invalid')
    return Binding(**{name: value[name] for name in fields if name != 'schema_version'})


def _connection(connection: socket.socket, *, expected_uid: int, expected_pid: int) -> None:
    if (connection.family != socket.AF_UNIX
            or connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
            or type(expected_uid) is not int or expected_uid < 0
            or type(expected_pid) is not int or expected_pid <= 0):
        raise ControlError('control_handoff_invalid')
    peer = peer_credentials(connection)
    if peer.uid != expected_uid or peer.pid != expected_pid:
        raise ControlError('control_peer_forbidden')


def _pipes(descriptors: tuple[int, ...]) -> None:
    if len(descriptors) != DESCRIPTORS or len(set(descriptors)) != DESCRIPTORS:
        raise ControlError('control_handoff_invalid')
    identities = set()
    for fd, mode in zip(descriptors, (os.O_RDONLY, os.O_WRONLY, os.O_RDONLY)):
        metadata = os.fstat(fd)
        identity = metadata.st_dev, metadata.st_ino
        if (not stat.S_ISFIFO(metadata.st_mode) or identity in identities
                or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != mode):
            raise ControlError('control_handoff_invalid')
        identities.add(identity)


def close_descriptors(descriptors) -> None:
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass


def send_offer(connection: socket.socket, binding: Binding, descriptors: tuple[int, int, int],
               *, expected_uid: int, expected_pid: int) -> None:
    """Send duplicates; the sender retains ownership of all original descriptors."""
    _connection(connection, expected_uid=expected_uid, expected_pid=expected_pid)
    _pipes(descriptors)
    raw = json.dumps(binding.payload(), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_OFFER:
        raise ControlError('control_handoff_invalid')
    sent = connection.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', descriptors))])
    if sent != len(raw):
        raise ControlError('control_handoff_incomplete')


def receive_offer(connection: socket.socket, expected: Binding, *, expected_uid: int,
                  expected_pid: int) -> tuple[int, int, int]:
    """Return owned CLOEXEC duplicates, closing every received FD on rejection."""
    _connection(connection, expected_uid=expected_uid, expected_pid=expected_pid)
    expected.payload()
    received = []
    try:
        raw, ancillary, flags, _ = connection.recvmsg(
            MAX_OFFER, socket.CMSG_SPACE(DESCRIPTORS * array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
        invalid_ancillary = False
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array('i')
                values.frombytes(data[:len(data) - len(data) % values.itemsize])
                received.extend(values)
                invalid_ancillary |= len(data) % values.itemsize != 0
            else:
                invalid_ancillary = True
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or invalid_ancillary or not raw:
            raise ControlError('control_handoff_incomplete')
        if parse_binding(decode(raw)) != expected:
            raise ControlError('control_handoff_changed')
        descriptors = tuple(received)
        _pipes(descriptors)
        if any(os.get_inheritable(fd) for fd in descriptors):
            raise ControlError('control_handoff_invalid')
        return descriptors
    except BaseException:
        close_descriptors(received)
        raise
