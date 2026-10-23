"""Supervisor custody of restore capability, ownership ACK and response-gate FDs.

Only the owned backend may offer channels. Only the exact operation-unit process
may collect them. Transport acknowledgement never substitutes for canonical
takeover or successful HTTP response completion. No runtime unit is started here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import stat
import time

from .descriptor_transport import Binding, MAX_OFFER, close_descriptors, parse_binding, receive_offer, send_offer
from .rpc import ControlError, Peer, decode, peer_credentials


def send_packet(connection: socket.socket, value: dict) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_OFFER or connection.send(raw) != len(raw):
        raise ControlError('control_handoff_incomplete')


def receive_packet(connection: socket.socket) -> dict:
    raw, ancillary, flags, _ = connection.recvmsg(MAX_OFFER, 0)
    # Metadata packets must never contain descriptors. With no ancillary buffer,
    # the kernel closes rejected SCM_RIGHTS duplicates and reports MSG_CTRUNC.
    if not raw or ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise ControlError('control_handoff_invalid')
    value = decode(raw)
    if 'error' in value:
        if set(value) != {'error'} or not isinstance(value['error'],str):
            raise ControlError('control_handoff_invalid')
        raise ControlError(value['error'])
    return value


def connect(path: Path, *, expected_uid: int, expected_pid: int) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    try:
        connection.settimeout(5)
        connection.connect(str(path))
        peer = peer_credentials(connection)
        if peer.uid != expected_uid or peer.pid != expected_pid:
            raise ControlError('control_peer_forbidden')
        return connection
    except BaseException:
        connection.close();raise


@dataclass
class Pending:
    binding: Binding
    descriptors: tuple[int, int, int]
    issuer: Peer
    expires_at: float


class Server:
    """One finite in-memory handoff; all authority remains in parent journals."""
    def __init__(self, path: Path, *, authorize_offer, authorize_collect, start_operation):
        self.path = path
        self.authorize_offer = authorize_offer
        self.authorize_collect = authorize_collect
        self.start_operation = start_operation
        self.pending: Pending | None = None
        self.consumed: set[tuple[str,int]] = set()
        self.listener = None
        self.identity = None
        self.accept_task = None
        self.tasks = set()
        self.connections = set()
        self.expiration = None
        self.closed = True

    async def start(self):
        if not self.closed or self.listener is not None:
            raise ControlError('control_handoff_busy')
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o077 or self.path.exists() or self.path.is_symlink()):
            raise ControlError('control_handoff_untrusted')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        try:
            listener.bind(str(self.path));os.chmod(self.path, 0o600)
            metadata = self.path.lstat();self.identity = metadata.st_dev, metadata.st_ino
            listener.listen(4);listener.setblocking(False)
            self.listener = listener;self.closed = False
            self.accept_task = asyncio.create_task(self._accept())
        except BaseException:
            listener.close();raise

    async def _accept(self):
        loop = asyncio.get_running_loop()
        while not self.closed:
            try:
                connection, _ = await loop.sock_accept(self.listener)
            except (OSError, asyncio.CancelledError):
                if self.closed:return
                raise
            if len(self.tasks) >= 4:
                connection.close();continue
            connection.settimeout(5)
            task = asyncio.create_task(self._handle(connection))
            self.tasks.add(task);task.add_done_callback(self.tasks.discard)

    def _expire(self):
        if self.pending is not None and time.monotonic() >= self.pending.expires_at:
            close_descriptors(self.pending.descriptors);self.pending = None

    async def _handle(self, connection):
        self.connections.add(connection)
        descriptors = None
        offered = None
        try:
            peer = peer_credentials(connection)
            if peer.uid != os.geteuid():raise ControlError('control_peer_forbidden')
            value = await asyncio.to_thread(receive_packet, connection)
            self._expire()
            if set(value) == {'schema_version', 'action', 'binding'} and value['action'] == 'offer':
                if type(value['schema_version']) is not int or value['schema_version'] != 1:
                    raise ControlError('control_handoff_invalid')
                binding = parse_binding(value['binding'])
                identity = binding.operation_id,binding.generation
                if self.pending is not None or identity in self.consumed:
                    raise ControlError('control_handoff_busy')
                await self.authorize_offer(binding, peer)
                await asyncio.to_thread(send_packet, connection, {'ready': True})
                descriptors = await asyncio.to_thread(receive_offer, connection, binding,
                                                       expected_uid=peer.uid, expected_pid=peer.pid)
                await self.authorize_offer(binding, peer)
                # No await between admission recheck and custody publication.
                if self.closed or self.pending is not None or identity in self.consumed:
                    raise ControlError('control_handoff_busy')
                # Fresh canonical admission proves an earlier operation cannot
                # collect again. Retain only the current identity's tombstone.
                self.consumed.intersection_update({identity})
                pending = Pending(binding, descriptors, peer, time.monotonic()+30)
                self.pending = pending;descriptors = None
                offered = pending
                self.expiration = asyncio.get_running_loop().call_later(30, self._expire)
                await self.start_operation(binding)
                if self.closed:raise ControlError('control_operator_stopped')
                await asyncio.to_thread(send_packet, connection, {'custody': True, 'request_id': binding.request_id})
            elif set(value) == {'schema_version', 'action', 'operation_id', 'generation', 'supervisor_invocation_id'} and value['action'] == 'collect':
                pending = self.pending
                if (type(value['schema_version']) is not int or value['schema_version'] != 1
                        or type(value['generation']) is not int or pending is None
                        or value['operation_id'] != pending.binding.operation_id
                        or value['generation'] != pending.binding.generation
                        or value['supervisor_invocation_id'] != pending.binding.supervisor_invocation_id):
                    raise ControlError('control_handoff_changed')
                await self.authorize_offer(pending.binding, pending.issuer)
                await self.authorize_collect(pending.binding, peer)
                await asyncio.to_thread(send_packet, connection, {'binding': pending.binding.payload()})
                if await asyncio.to_thread(receive_packet, connection) != {'ready': True}:
                    raise ControlError('control_handoff_invalid')
                self._expire()
                await self.authorize_offer(pending.binding, pending.issuer)
                await self.authorize_collect(pending.binding, peer)
                if self.closed or self.pending is not pending:
                    raise ControlError('control_handoff_changed')
                # Consume before dispatch; a lost delivery cannot replay a
                # capability or fabricate a second controller acknowledgement.
                self.pending = None;self.consumed.add((pending.binding.operation_id,pending.binding.generation))
                if self.expiration is not None:self.expiration.cancel();self.expiration = None
                descriptors = pending.descriptors
                await asyncio.to_thread(send_offer, connection, pending.binding, descriptors,
                                         expected_uid=peer.uid, expected_pid=peer.pid)
            else:
                raise ControlError('control_handoff_invalid')
        except Exception as exc:
            if offered is not None and self.pending is offered:
                self.pending = None;close_descriptors(offered.descriptors)
                if self.expiration is not None:self.expiration.cancel();self.expiration = None
            code = exc.code if isinstance(exc, ControlError) else 'control_handoff_failed'
            try:await asyncio.to_thread(send_packet, connection, {'error': code})
            except (OSError, ControlError):pass
        finally:
            if descriptors is not None:close_descriptors(descriptors)
            connection.close();self.connections.discard(connection)

    async def close(self):
        self.closed = True
        if self.expiration is not None:self.expiration.cancel();self.expiration = None
        if self.accept_task is not None:
            self.accept_task.cancel()
            await asyncio.gather(self.accept_task, return_exceptions=True)
        if self.listener is not None:self.listener.close()
        for connection in tuple(self.connections):
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        if self.tasks:await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.pending is not None:
            close_descriptors(self.pending.descriptors);self.pending = None
        if self.identity is not None:
            metadata = self.path.lstat()
            if (metadata.st_dev, metadata.st_ino) != self.identity or not stat.S_ISSOCK(metadata.st_mode):
                raise ControlError('control_handoff_changed')
            self.path.unlink()
        self.listener = None;self.identity = None
