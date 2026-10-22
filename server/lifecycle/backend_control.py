"""Authenticated local owner/controller requests to the actual live coordinator."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from . import state
from .bootstrap import current_readiness_projection, get_startup_readiness
from .control.rpc import ControlError, Peer, Server
from .coordination import EffectClass, get_runtime_coordinator
from .execution_state import read_active_execution
from .layout import layout_for_root
from .runtime_admission import require_runtime_admission
from .setup_bootstrap import SetupAuthorityError, require_setup_authority


class BackendControl:
    def __init__(self, root: Path, invocation: str):
        self.root, self.layout, self.invocation = root, layout_for_root(root), invocation
        descriptor = self.layout.descriptor.descriptor
        self.installation_id = descriptor.installation_id
        self.server = Server(
            self.layout.runtime / "backend.sock", self.installation_id, invocation,
            self.handle, allowed_uids=frozenset({0, descriptor.service_uid}),
        )
        self.preparation = {}
        self.drains = {}
        self.cache_preparation = {}

    @staticmethod
    def preparation_members(preparation_id: str) -> set[int]:
        path = Path(f'/sys/fs/cgroup/system.slice/streamhome-prepare@{preparation_id}.service/cgroup.procs')
        try:
            return {int(pid) for pid in path.read_text().split()}
        except FileNotFoundError:
            return set()

    def release_cache_record(self, preparation_id: str, record: dict) -> None:
        if self.cache_preparation.get(preparation_id) is not record:
            raise ControlError('control_preparation_changed')
        record['lease'].detach_child(record['child'])
        record['lease'].release()
        del self.cache_preparation[preparation_id]

    async def watch_cache_preparation(self, preparation_id: str, record: dict) -> None:
        from .control.administration_lock import process_identity
        while self.cache_preparation.get(preparation_id) is record:
            try:
                start, status = process_identity(record['pid'])
                alive = start == record['start'] and status not in {'Z', 'X'}
            except FileNotFoundError:
                alive = False
            # A dead builder leader alone cannot release custody of detached
            # npm/FFmpeg-style sessions still living in the same owned unit.
            if not alive and not self.preparation_members(preparation_id):
                self.release_cache_record(preparation_id, record)
                return
            await asyncio.sleep(0.25)

    async def cache_preparation_action(self, peer: Peer, action: str, arguments: dict) -> dict:
        from .control.runtime_preparation import grant_sha256, read_grant, require_current
        from .control.processes import process_cgroup
        from .management_journal import _uuid
        if set(arguments) != {'preparation_id', 'grant_sha256'} or peer.uid != os.geteuid():
            raise ControlError('control_preparation_invalid')
        preparation_id = _uuid(arguments['preparation_id'], 'cache preparation')
        if process_cgroup(peer.pid) != f'/system.slice/streamhome-prepare@{preparation_id}.service':
            raise ControlError('control_preparation_forbidden')
        record = self.cache_preparation.get(preparation_id)
        if action == 'register_cache_preparation':
            value = read_grant(preparation_id)
            if (grant_sha256(value) != arguments['grant_sha256'] or value['installation_id'] != self.installation_id
                    or value['backend']['pid'] != os.getpid() or value['backend']['start_identity'] != state._process_start_identity(os.getpid())
                    or value['supervisor_invocation_id'] != self.invocation):
                raise ControlError('control_preparation_changed')
            require_current(value)
            if not require_runtime_admission().admitted():
                raise ControlError('control_runtime_not_settled')
            if record is None:
                lease = get_runtime_coordinator().admit('runtime.disposable-preparation', [EffectClass.DISPOSABLE_CACHE_WRITE])
                try:
                    child = lease.attach_child(peer.pid)
                except BaseException:
                    lease.release()
                    raise
                record = {'lease': lease, 'child': child, 'pid': peer.pid, 'start': state._process_start_identity(peer.pid),
                          'grant_sha256': arguments['grant_sha256'], 'grant': value}
                self.cache_preparation[preparation_id] = record
                record['task'] = asyncio.create_task(self.watch_cache_preparation(preparation_id, record), name='runtime-preparation-custody')
        if (record is None or record['pid'] != peer.pid or record['start'] != state._process_start_identity(peer.pid)
                or record['grant_sha256'] != arguments['grant_sha256']):
            raise ControlError('control_preparation_changed')
        if action == 'release_cache_preparation':
            if self.preparation_members(preparation_id) != {peer.pid}:
                raise ControlError('control_preparation_busy')
            self.release_cache_record(preparation_id, record)
            record['task'].cancel()
            await asyncio.gather(record['task'], return_exceptions=True)
            return {'released': True}
        require_current(record['grant'])
        return {'registered': True}

    def _controller(self, peer: Peer, arguments: dict, *, preserving_recovery: bool = False) -> tuple[str, int]:
        if set(arguments) != {"operation_id", "generation"}:
            raise ControlError("control_message_invalid")
        lease = state.read_controller_lease(self.root)
        operation = state.inspect_state(self.root)
        if (
            lease is None or not state.controller_lease_is_current(lease) or lease.pid != peer.pid
            or lease.operation_id != arguments["operation_id"] or lease.generation != arguments["generation"]
            or type(arguments["generation"]) is not int
            or operation.active_operation is None or operation.generation != lease.generation
            or operation.active_operation.operation_id != lease.operation_id
            or operation.active_operation.operation_type is not state.OperationType.MANAGEMENT
            or operation.active_operation.phase is not (state.LifecyclePhase.RECOVERY_REQUIRED if preserving_recovery else state.LifecyclePhase.RESERVED)
        ):
            raise ControlError("control_controller_changed")
        cgroups = Path(f"/proc/{peer.pid}/cgroup").read_text().splitlines()
        if f"0::/system.slice/streamhome-operation@{lease.operation_id}.service" not in cgroups:
            raise ControlError("control_controller_forbidden")
        return lease.operation_id, lease.generation

    async def handle(self, peer: Peer, action: str, arguments: dict) -> dict:
        if action in {'register_cache_preparation', 'cache_preparation_status', 'release_cache_preparation'}:
            return await self.cache_preparation_action(peer, action, arguments)
        if action == "readiness":
            # This narrow projection belongs to the backend's actual parent,
            # never another process running as the service account. It cannot
            # issue bootstrap codes, claim authority or acknowledge a handoff.
            from .control.processes import RUNTIME_CGROUP, process_cgroup
            if (arguments or peer.uid != os.geteuid() or peer.pid != os.getppid()
                    or process_cgroup(peer.pid) != RUNTIME_CGROUP):
                raise ControlError("control_peer_forbidden")
            from .management_journal import execution_tuple_sha256
            admission = require_runtime_admission()
            return {"pid": os.getpid(), "start_identity": state._process_start_identity(os.getpid()),
                    "execution_sha256": execution_tuple_sha256(admission.execution),
                    "readiness": current_readiness_projection(get_startup_readiness())}
        if action in {"snapshot", "bootstrap"}:
            if peer.uid != 0:
                raise ControlError("control_peer_forbidden")
            if action == "bootstrap":
                if set(arguments) != {"renew"} or type(arguments["renew"]) is not bool:
                    raise ControlError("control_message_invalid")
                readiness = get_startup_readiness().projection()
                admission = require_runtime_admission()
                if not readiness["generalReady"] or not admission.admitted():
                    raise ControlError("control_runtime_busy")
                try:
                    code = await require_setup_authority().issue(renew=arguments["renew"])
                except SetupAuthorityError as exc:
                    raise ControlError(exc.code) from exc
                return {"code": code.code, "expiresAt": code.expires_at, "remainingSeconds": code.remaining_seconds}
            if arguments:
                raise ControlError("control_message_invalid")
            from .management_journal import execution_tuple_payload
            admission = require_runtime_admission()
            return {
                "admitted": admission.admitted(), "pid": os.getpid(), "startIdentity": state._process_start_identity(os.getpid()),
                "installationId": self.installation_id, "invocation": self.invocation,
                "generation": state.inspect_state(self.root).generation,
                "execution": execution_tuple_payload(read_active_execution(self.root).execution),
                "readiness": get_startup_readiness().projection(),
            }
        operation_id, generation = self._controller(peer, arguments, preserving_recovery=action == 'ack_recovered_continuation')
        if action == 'ack_recovered_continuation':
            return require_runtime_admission().acknowledge_recovered_continuation(operation_id, controller_pid=peer.pid)
        if action in {"ack_continuation", "register_preparation"}:
            if operation_id in self.preparation:
                return {"acknowledged": True, "generation": generation}
            acknowledgement = require_runtime_admission().acknowledge_continuation(
                operation_id, controller_pid=peer.pid, preparation=action == "register_preparation",
            )
            if action == "register_preparation":
                lease = get_runtime_coordinator().admit("management.preparation", [EffectClass.CONFIG_WRITE, EffectClass.DISPOSABLE_CACHE_WRITE])
                child = lease.attach_child(peer.pid)
                self.preparation[operation_id] = (lease, child)
            return acknowledgement
        if action == "release_preparation":
            record = self.preparation.get(operation_id)
            if record is None:
                return {"released": True}
            processes = Path(f"/sys/fs/cgroup/system.slice/streamhome-operation@{operation_id}.service/cgroup.procs").read_text().split()
            if {int(pid) for pid in processes} != {peer.pid}:
                raise ControlError("control_preparation_busy")
            lease, child = record
            lease.detach_child(child)
            lease.release()
            del self.preparation[operation_id]
            return {"released": True}
        if action == "drain":
            if operation_id in self.preparation:
                raise ControlError("control_preparation_busy")
            coordinator = get_runtime_coordinator()
            snapshot = self.drains.get(operation_id)
            if snapshot is None:
                snapshot = coordinator.close_admission(list(EffectClass), operation_id=operation_id, generation=generation)
                self.drains[operation_id] = snapshot
            import services.state as runtime_state
            runtime_state.MAINTENANCE_MODE = True
            runtime_state.MAINTENANCE_REASON = "StreamHome is applying a reviewed configuration or runtime change."
            result = await coordinator.drain(snapshot, timeout=45.0)
            return {"drained": result.complete}
        raise ControlError("control_action_unsupported")

    async def start(self) -> None:
        await self.server.start()

    async def close(self) -> None:
        await self.server.close()
        tasks = [record['task'] for record in self.cache_preparation.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Backend shutdown never fabricates quiescence or detaches live work.
        # The independent preparation unit retains process custody and aborts
        # when its next bounded acknowledgement cannot reach this backend.
