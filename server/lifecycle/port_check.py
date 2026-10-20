"""Read-only listener preflight; never stops or signals a conflicting process.

A successful probe is advisory. The activation controller must recheck and then
prove actual listener/build readiness. Reuse needs independently selected exact
managed process evidence; a configured port number never proves ownership.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import ipaddress
import os
from pathlib import Path
import re
import socket
import sys

from . import state
from .installation import InstallationDescriptor, validate_managed_port


class ListenerCheckError(ValueError):
    pass


@dataclass(frozen=True)
class OwnedWebListener:
    installation_id: str
    service_uid: int
    supervisor_invocation_id: str
    pid: int
    process_start_identity: str
    bind: str
    port: int


@dataclass(frozen=True)
class PortCheck:
    bind: str
    port: int
    reusable_owned_listener: bool


def _proc_bytes(path: Path, maximum: int) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ListenerCheckError("Listener evidence exceeds its inspection bound.")
    return raw


def _process_matches(owner: OwnedWebListener, installation: InstallationDescriptor) -> bool:
    if (
        owner.installation_id != installation.installation_id or owner.service_uid != installation.service_uid
        or type(owner.pid) is not int or owner.pid <= 1
        or not re.fullmatch(r"[0-9a-f]{32}", owner.supervisor_invocation_id)
        or int(owner.supervisor_invocation_id, 16) == 0
    ):
        return False
    directory = Path("/proc") / str(owner.pid)
    try:
        if directory.stat().st_uid != owner.service_uid or state._process_start_identity(owner.pid) != owner.process_start_identity:
            return False
        if os.readlink(directory / "ns/net") != os.readlink("/proc/self/ns/net"):
            return False
        cgroups = _proc_bytes(directory / "cgroup", 16384).decode("ascii").splitlines()
        if "0::/system.slice/streamhome.service" not in cgroups:
            return False
        environment = {}
        keys = {b"INVOCATION_ID", b"STREAMHOME_INSTALLATION_ID", b"STREAMHOME_SERVICE"}
        for item in _proc_bytes(directory / "environ", 1024 * 1024).split(b"\0"):
            key, separator, value = item.partition(b"=")
            if separator and key in keys:
                if key in environment:
                    return False
                environment[key] = value
        return environment == {
            b"INVOCATION_ID": owner.supervisor_invocation_id.encode("ascii"),
            b"STREAMHOME_INSTALLATION_ID": owner.installation_id.encode("ascii"),
            b"STREAMHOME_SERVICE": b"web",
        }
    except (OSError, ValueError, UnicodeError, state.LifecycleError):
        return False


def _socket_inodes(bind: str, port: int) -> set[str]:
    result: set[str] = set()
    for family, table in ((socket.AF_INET, "tcp"), (socket.AF_INET6, "tcp6")):
        path = Path("/proc/self/net") / table
        try:
            raw = _proc_bytes(path, 8 * 1024 * 1024).decode("ascii")
        except FileNotFoundError:
            if family == socket.AF_INET6 and not socket.has_ipv6:
                continue
            raise ListenerCheckError("Kernel listener evidence is unavailable.")
        for line in raw.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            encoded, separator, number = fields[1].partition(":")
            if not separator or int(number, 16) != port:
                continue
            # proc formats each 32-bit word in native host order.
            octets = b"".join(int(encoded[index:index + 8], 16).to_bytes(4, sys.byteorder) for index in range(0, len(encoded), 8))
            address = ipaddress.ip_address(octets)
            if isinstance(address, ipaddress.IPv6Address):
                if address.is_unspecified:
                    # IPv6 wildcard may be dual-stack. Never guess IPV6_V6ONLY
                    # from a configured port or from host-wide defaults.
                    overlaps = True
                elif address.ipv4_mapped is not None:
                    overlaps = bind == "0.0.0.0" or str(address.ipv4_mapped) == bind
                else:
                    overlaps = False
            else:
                overlaps = bind == "0.0.0.0" or address.is_unspecified or str(address) == bind
            if overlaps:
                if not fields[9].isdigit() or fields[9] == "0":
                    raise ListenerCheckError("Kernel socket ownership is incomplete.")
                result.add(fields[9])
    return result


def _owns_all_sockets(owner: OwnedWebListener, inodes: set[str]) -> bool:
    if not inodes:
        return False
    targets = {f"socket:[{inode}]" for inode in inodes}
    observed: set[str] = set()
    try:
        for descriptor in (Path("/proc") / str(owner.pid) / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target in targets:
                observed.add(target)
    except OSError:
        return False
    return observed == targets


def check_web_port(
    bind: str, port: int | str, *, owner: OwnedWebListener | None = None,
    installation: InstallationDescriptor | None = None,
) -> PortCheck:
    selected = validate_managed_port(port)
    if bind not in {"127.0.0.1", "0.0.0.0"}:
        raise ListenerCheckError("The managed web binding is unsupported.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            # No REUSEADDR/REUSEPORT: an occupied endpoint must not appear free.
            probe.bind((bind, selected))
            probe.listen(1)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise ListenerCheckError("The requested web listener cannot be bound.") from exc
        else:
            return PortCheck(bind, selected, False)
    if owner is not None and installation is not None and owner.bind in {"127.0.0.1", "0.0.0.0"} and owner.port == selected:
        try:
            if _process_matches(owner, installation):
                before = _socket_inodes(bind, selected)
                if _owns_all_sockets(owner, before) and _process_matches(owner, installation) and before == _socket_inodes(bind, selected):
                    return PortCheck(bind, selected, True)
        except (OSError, ValueError):
            pass
    raise ListenerCheckError("The selected web port is in use; no other process was stopped. Choose another port or verify exact installation ownership.")
