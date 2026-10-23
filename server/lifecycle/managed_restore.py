"""Managed restore operation-unit enrollment; never launch detached app children."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import uuid

from . import state
from .layout import layout_for_root
from .parent_execution import parse_parent_execution
from .control.descriptor_transport import Binding, close_descriptors, parse_binding, receive_offer, send_offer
from .control.parent_handoff import connect, receive_packet, send_packet
from .control.processes import controller_peer, process_cgroup, RUNTIME_CGROUP
from .control.rpc import ControlError


def bound_journal(root: Path, operation_id: str | None = None):
    from .restore_controller import inspect_restore_journal
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if (operation is None or operation.operation_type is not state.OperationType.RESTORE
            or operation_id is not None and operation.operation_id != operation_id):
        raise ControlError('control_authority_changed')
    journal = inspect_restore_journal(root, operation.operation_id)
    if (operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or journal['schema_version'] != 2 or journal['generation'] != authority.generation):
        raise ControlError('control_parent_binding_changed')
    bound = parse_parent_execution(journal['managed_execution'])
    layout = layout_for_root(root)
    from .execution_state import read_active_execution
    if (not layout.managed or layout.descriptor.descriptor.installation_id != bound.installation_id
            or layout.descriptor.descriptor.service_uid != os.geteuid() or os.geteuid() == 0
            or read_active_execution(root).execution != bound.execution):
        raise ControlError('control_parent_binding_changed')
    return journal,bound


def _endpoint(root: Path, bound):
    from .control.privileged import unit_snapshot
    from .control.administration_lock import process_identity
    layout = layout_for_root(root)
    actor = unit_snapshot()
    start, status = process_identity(bound.supervisor_pid)
    if (not layout.managed or os.geteuid() == 0 or layout.descriptor.descriptor.service_uid != os.geteuid()
            or layout.descriptor.descriptor.installation_id != bound.installation_id
            or actor['ActiveState'] != 'active' or int(actor['MainPID']) != bound.supervisor_pid
            or actor['InvocationID'] != bound.supervisor_invocation_id
            or start != bound.supervisor_start_identity or status in {'Z','X'}
            or process_cgroup(bound.supervisor_pid) != RUNTIME_CGROUP):
        raise ControlError('control_parent_binding_changed')
    return layout.runtime / 'parent-handoff.sock'


def command(root: Path,action: str) -> dict:
    from .control.rpc import request
    journal,bound = bound_journal(root)
    lease = state.read_controller_lease(root)
    if (lease is None or lease.pid != os.getpid() or lease.operation_id != journal['operation_id']
            or lease.generation != journal['generation'] or not state.controller_lease_is_current(lease)):
        raise ControlError('control_controller_changed')
    controller_peer(os.getpid(),journal['operation_id'])
    _endpoint(root,bound)
    return request(layout_for_root(root).runtime/'supervisor.sock',expected_uid=os.geteuid(),
        expected_pid=bound.supervisor_pid,installation_id=bound.installation_id,invocation=bound.supervisor_invocation_id,
        action=action,arguments={'operation_id':journal['operation_id'],'generation':journal['generation']},timeout=60)


def deployment(root: Path):
    from .configuration_store import ConfigurationStore
    journal,bound = bound_journal(root)
    layout = layout_for_root(root)
    directory = os.open(layout.domains,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        with ConfigurationStore(directory,bound.installation_id) as store:
            manifest = store.inspect()
            if manifest.active.revision != bound.execution.deployment_revision:
                raise ControlError('control_parent_binding_changed')
            return store.read_revision(manifest.active).settings
    finally:os.close(directory)


@dataclass
class ManagedProcess:
    pid: int
    start: str
    operation_id: str

    def poll(self):
        from .control.administration_lock import process_identity
        try:
            start, status = process_identity(self.pid)
            return None if start == self.start and status not in {'Z','X'} else 1
        except FileNotFoundError:return 1

    def terminate(self):
        from .control.administration_lock import process_identity
        try:descriptor = os.pidfd_open(self.pid)
        except ProcessLookupError:return
        try:
            start, status = process_identity(self.pid)
            if start != self.start or status in {'Z','X'}:return
            controller_peer(self.pid, self.operation_id)
            if Path(f'/proc/{self.pid}').stat().st_uid != os.geteuid():
                raise ControlError('control_controller_forbidden')
            signal.pidfd_send_signal(descriptor,signal.SIGTERM)
        finally:os.close(descriptor)


@dataclass
class ManagedChild(ManagedProcess):
    def terminate(self):
        raise ControlError('control_supervisor_custody_required')


def spawn(root: Path, *, operation_id: str, generation: int, owner_capability: str, timeout: float):
    from .restore_controller import RestoreConflictError, RestoreHandoff, _write_all
    journal, bound = bound_journal(root, operation_id)
    if (journal['generation'] != generation or bound.original_generation != generation
            or process_cgroup(os.getpid()) != RUNTIME_CGROUP or not 0 < timeout <= 30):
        raise RestoreConflictError('managed restore handoff identity is unavailable')
    binding = Binding(bound.installation_id, operation_id, generation, bound.supervisor_invocation_id,
                      str(uuid.uuid4()), 'restore')
    ack_read, ack_write = os.pipe();cap_read, cap_write = os.pipe();gate_read, gate_write = os.pipe()
    descriptors = [ack_read,ack_write,cap_read,cap_write,gate_read,gate_write]
    process = None
    try:
        endpoint = _endpoint(root,bound)
        with connect(endpoint,expected_uid=os.geteuid(),expected_pid=bound.supervisor_pid) as connection:
            send_packet(connection,{'schema_version':1,'action':'offer','binding':binding.payload()})
            if receive_packet(connection) != {'ready':True}:
                raise ControlError('control_handoff_invalid')
            _write_all(cap_write,(owner_capability+'\n').encode('ascii'))
            os.close(cap_write);descriptors.remove(cap_write)
            send_offer(connection,binding,(cap_read,ack_write,gate_read),expected_uid=os.geteuid(),expected_pid=bound.supervisor_pid)
            if receive_packet(connection) != {'custody':True,'request_id':binding.request_id}:
                raise ControlError('control_handoff_invalid')
        for fd in (cap_read,ack_write,gate_read):os.close(fd);descriptors.remove(fd)
        if not select.select([ack_read],[],[],timeout)[0]:
            raise RestoreConflictError('managed restore ownership was not acknowledged')
        from .control.rpc import decode
        response = decode(os.read(ack_read,4097))
        if (set(response) != {'owned','operation_id','generation'} or response['owned'] is not True
                or response['operation_id'] != operation_id or type(response['generation']) is not int
                or response['generation'] != generation+1):
            raise RestoreConflictError('managed restore ownership acknowledgement is invalid')
        lease = state.read_controller_lease(root)
        if (lease is None or lease.operation_id != operation_id or lease.generation != generation+1
                or lease.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER or not state.controller_lease_is_current(lease)):
            raise RestoreConflictError('managed restore controller lease is unavailable')
        controller_peer(lease.pid,operation_id)
        process = ManagedProcess(lease.pid,lease.process_start_identity,operation_id)
        state.validate_operation_with_credential(root,operation_id=operation_id,generation=generation+1)
        descriptors.remove(gate_write)
        return RestoreHandoff(process,operation_id,generation+1,gate_write)
    except Exception as exc:
        if process is not None:process.terminate()
        if isinstance(exc,RestoreConflictError):raise
        raise RestoreConflictError('managed restore handoff failed') from exc
    finally:close_descriptors(descriptors)


def execute(root: Path, operation_id: str, *, control_manifest_sha256: str) -> int:
    from .restore_controller import _read_pipe_secret, _run_controller
    controller_peer(os.getpid(),operation_id)
    journal, bound = bound_journal(root,operation_id)
    if (bound.execution.control_manifest_sha256 != control_manifest_sha256
            or journal['generation'] != bound.original_generation):
        raise ControlError('control_parent_binding_changed')
    endpoint = _endpoint(root,bound)
    with connect(endpoint,expected_uid=os.geteuid(),expected_pid=bound.supervisor_pid) as connection:
        send_packet(connection,{'schema_version':1,'action':'collect','operation_id':operation_id,
            'generation':journal['generation'],'supervisor_invocation_id':bound.supervisor_invocation_id})
        response = receive_packet(connection)
        if set(response) != {'binding'}:raise ControlError('control_handoff_invalid')
        binding = parse_binding(response['binding'])
        if (binding.installation_id != bound.installation_id or binding.operation_id != operation_id
                or binding.operation_type != 'restore' or binding.generation != journal['generation']
                or binding.supervisor_invocation_id != bound.supervisor_invocation_id):
            raise ControlError('control_handoff_changed')
        send_packet(connection,{'ready':True})
        descriptors = receive_offer(connection,binding,expected_uid=os.geteuid(),expected_pid=bound.supervisor_pid)
    cap,ack,gate = descriptors
    identities = {fd:(os.fstat(fd).st_dev,os.fstat(fd).st_ino) for fd in descriptors}
    try:
        capability = _read_pipe_secret(cap)
        # Existing controller owns transfer, durable generation publication, ACK
        # and the actual ASGI response gate. Transport never performs those steps.
        return _run_controller(root,operation_id,journal['generation'],capability,
                               acknowledgement_fd=ack,gate_fd=gate)
    finally:
        # Existing controller helpers consume/close individual channels. Their
        # descriptor numbers can be reused for journal/SQLite FDs before return;
        # never blindly close a reused number in this wrapper's cleanup.
        for fd,identity in identities.items():
            try:
                current = os.fstat(fd)
                if (current.st_dev,current.st_ino) == identity:os.close(fd)
            except OSError:pass
