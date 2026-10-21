"""Bounded local RPC with kernel peer credentials, no shell or dynamic dispatch."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time
from typing import Any, Awaitable, Callable
import uuid


MAX_MESSAGE = 32 * 1024


class ControlError(RuntimeError):
    def __init__(self, code: str = "control_unavailable"):
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "control_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int


def peer_credentials(connection: socket.socket) -> Peer:
    pid, uid, gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    if pid <= 0 or uid < 0 or gid < 0:
        raise ControlError("control_peer_invalid")
    return Peer(pid, uid, gid)


def encode(payload: dict) -> bytes:
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ControlError("control_message_invalid") from exc
    if not 0 < len(raw) <= MAX_MESSAGE:
        raise ControlError("control_message_too_large")
    return struct.pack("!I", len(raw)) + raw


def decode(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ControlError("control_message_invalid")
            result[key] = value
        return result

    def reject(value):
        raise ControlError("control_message_invalid")

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ControlError("control_message_invalid") from exc
    if not isinstance(payload, dict):
        raise ControlError("control_message_invalid")
    return payload


def validate_request(payload: dict) -> dict:
    if (
        set(payload) != {"schema_version", "request_id", "installation_id", "invocation", "action", "arguments"}
        or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
        or not isinstance(payload["action"], str) or not re.fullmatch(r"[a-z_]{1,48}", payload["action"])
        or not isinstance(payload["arguments"], dict)
    ):
        raise ControlError("control_message_invalid")
    for name in ("request_id", "installation_id"):
        try:
            identity = uuid.UUID(payload[name]) if isinstance(payload[name], str) else None
        except ValueError as exc:
            raise ControlError("control_message_invalid") from exc
        if identity is None or not identity.int or str(identity) != payload[name]:
            raise ControlError("control_message_invalid")
    if not isinstance(payload["invocation"], str) or not re.fullmatch(r"[0-9a-f]{32}", payload["invocation"]) or int(payload["invocation"], 16) == 0:
        raise ControlError("control_message_invalid")
    return payload


def request(
    path: Path, *, expected_uid: int, installation_id: str, invocation: str,
    action: str, arguments: dict, expected_pid: int | None = None, timeout: float = 15.0,
) -> dict:
    payload = validate_request({"schema_version": 1, "request_id": str(uuid.uuid4()), "installation_id": installation_id,
                                "invocation": invocation, "action": action, "arguments": arguments})
    if not 0 < timeout <= 65:
        raise ControlError("control_timeout_invalid")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            deadline = time.monotonic() + timeout
            connection.settimeout(timeout)
            connection.connect(str(path))
            peer = peer_credentials(connection)
            if peer.uid != expected_uid or expected_pid is not None and peer.pid != expected_pid:
                raise ControlError("control_peer_forbidden")
            connection.sendall(encode(payload))

            def read_exact(length):
                result = bytearray()
                while len(result) < length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ControlError("control_timeout")
                    connection.settimeout(remaining)
                    block = connection.recv(length-len(result))
                    if not block:
                        raise ControlError("control_reply_incomplete")
                    result.extend(block)
                return bytes(result)

            length = struct.unpack("!I", read_exact(4))[0]
            if not 0 < length <= MAX_MESSAGE:
                raise ControlError("control_reply_invalid")
            response = decode(read_exact(length))
            if set(response) != {"schema_version", "request_id", "ok", "result", "error"} or type(response["schema_version"]) is not int or response["schema_version"] != 1 or response["request_id"] != payload["request_id"] or type(response["ok"]) is not bool:
                raise ControlError("control_reply_invalid")
            if not response["ok"]:
                raise ControlError(response["error"] if isinstance(response["error"], str) else "control_unavailable")
            if response["error"] is not None or not isinstance(response["result"], dict):
                raise ControlError("control_reply_invalid")
            return response["result"]
    except OSError as exc:
        raise ControlError("control_unavailable") from exc


Handler = Callable[[Peer, str, dict], Awaitable[dict]]


class Server:
    def __init__(self, path: Path, installation_id: str, invocation: str, handler: Handler, *, allowed_uids: frozenset[int], bridge_gid: int | None = None):
        validate_request({"schema_version": 1, "request_id": str(uuid.uuid4()), "installation_id": installation_id,
                          "invocation": invocation, "action": "status", "arguments": {}})
        self.path, self.installation_id, self.invocation = path, installation_id, invocation
        self.handler, self.allowed_uids = handler, allowed_uids
        if bridge_gid is not None and (os.geteuid() != 0 or type(bridge_gid) is not int or bridge_gid <= 0):
            raise ControlError("control_directory_untrusted")
        self.bridge_gid = bridge_gid
        self.server = None
        self.connections: set[asyncio.Task] = set()
        self.limit = asyncio.Semaphore(8)

    async def start(self) -> None:
        parent = self.path.parent.lstat()
        private = self.bridge_gid is None and not parent.st_mode & 0o7077
        bridge = self.bridge_gid is not None and parent.st_uid == 0 and parent.st_gid == self.bridge_gid and stat.S_IMODE(parent.st_mode) == 0o710
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or not (private or bridge):
            raise ControlError("control_directory_untrusted")
        # RuntimeDirectory cleanup belongs to the supervisor/unit. Never unlink
        # a possibly live or foreign socket just to force another server up.
        if self.path.exists() or self.path.is_symlink():
            raise ControlError("control_socket_exists")
        self.server = await asyncio.start_unix_server(self._connection, path=str(self.path), limit=MAX_MESSAGE+4)
        if self.bridge_gid is None:
            os.chmod(self.path, 0o600, follow_symlinks=False)
        else:
            os.chown(self.path, 0, self.bridge_gid, follow_symlinks=False)
            os.chmod(self.path, 0o660, follow_symlinks=False)
        self.identity = self.path.lstat()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.connections.add(task)
        identity = None
        try:
            peer = peer_credentials(writer.get_extra_info("socket"))
            if peer.uid not in self.allowed_uids:
                raise ControlError("control_peer_forbidden")
            async with self.limit:
                async with asyncio.timeout(60):
                    length = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 5))[0]
                    if not 0 < length <= MAX_MESSAGE:
                        raise ControlError("control_message_too_large")
                    payload = validate_request(decode(await asyncio.wait_for(reader.readexactly(length), 5)))
                    identity = payload["request_id"]
                    if payload["installation_id"] != self.installation_id or payload["invocation"] != self.invocation:
                        raise ControlError("control_incarnation_changed")
                    result = await self.handler(peer, payload["action"], payload["arguments"])
                    writer.write(encode({"schema_version": 1, "request_id": identity, "ok": True, "result": result, "error": None}))
                    await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ControlError) else "control_unavailable"
            try:
                writer.write(encode({"schema_version": 1, "request_id": identity, "ok": False, "result": None, "error": code}))
                await writer.drain()
            except (OSError, ControlError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.connections.discard(task)

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.connections):
            task.cancel()
        if self.connections:
            await asyncio.gather(*tuple(self.connections), return_exceptions=True)
        try:
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) == (self.identity.st_dev, self.identity.st_ino):
                self.path.unlink()
        except FileNotFoundError:
            pass
