"""Managed UPDATE preparation under the existing updater's controller lease.

The original update journal owns every tuple. These helpers neither reserve a
MANAGEMENT operation nor authorize systemd/runtime start or database mutation.
The production update handoff remains gated until its complete reader protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import fcntl
import os
import time
from pathlib import Path

from . import state, update_controller
from .execution_state import read_active_execution
from .layout import layout_for_root
from .management_journal import execution_tuple_payload
from .runtime_build import PreparedRuntime, prepare_runtime, preparation_source, verify_runtime
from . import update_execution
from .control.rpc import ControlError


def bound_journal(root: Path, operation_id: str):
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if (authority.schema_version != 2 or operation is None or operation.operation_id != operation_id
            or operation.operation_type is not state.OperationType.UPDATE
            or operation.owner_kind is not state.OwnerKind.UPDATER):
        raise ControlError('control_authority_changed')
    path, _, _ = update_controller._update_paths(root, operation_id, authority.generation)
    journal = update_controller.read_update_journal(path)
    update_controller._validate_update_journal_semantics(journal, authority=authority)
    bound = update_execution.parse(journal)
    layout = layout_for_root(root)
    if (bound is None or not layout.managed or os.geteuid() == 0
            or layout.descriptor.descriptor.service_uid != os.geteuid()
            or layout.descriptor.descriptor.installation_id != bound.parent.installation_id):
        raise ControlError('control_parent_binding_changed')
    return authority, journal, bound


def read_fence_binding(root: Path,operation_id: str):
    """Read only a finite original-owner phase gap for stopping dead control.

    The existing supervisor separately proves its exact parent/OS relationship;
    the fencing caller holds controller.lock and proves the matching lease dead.
    This projection grants no transfer, update progress, acceptance or start.
    """
    try:return bound_journal(root,operation_id)
    except update_controller.UpdateControllerConflict:
        authority=state.inspect_state(root);operation=authority.active_operation
        if (authority.schema_version!=2 or operation is None or operation.operation_id!=operation_id
                or operation.operation_type is not state.OperationType.UPDATE
                or operation.owner_kind is not state.OwnerKind.UPDATER):
            raise ControlError('control_authority_changed')
        if operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
            from .update_recovery_observation import read_binding
            observed,journal,bound,_=read_binding(root,operation_id)
            return observed,journal,bound
        path,_,_=update_controller._update_paths(root,operation_id,authority.generation)
        journal=update_controller.read_update_journal(path);bound=update_execution.parse(journal)
        layout=layout_for_root(root)
        if (operation.phase is not state.LifecyclePhase.VERIFYING or journal.get('lifecycle_phase')!='exclusive'
                or journal.get('phase')!='starting' or journal.get('lifecycle_generation')!=authority.generation
                or journal.get('runtime_committed') is not False or bound is None or bound.accepted is not None
                or bound.parent.original_generation+1!=authority.generation
                or not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid()
                or layout.descriptor.descriptor.installation_id!=bound.parent.installation_id):
            raise ControlError('control_update_phase_gap_unproven')
        prior=replace(authority,active_operation=replace(operation,phase=state.LifecyclePhase.EXCLUSIVE))
        update_controller._validate_update_journal_semantics(journal,authority=prior)
        if state.inspect_state(root)!=authority or update_controller.read_update_journal(path)!=journal:
            raise ControlError('control_authority_changed')
        return authority,journal,bound


def fence_dead_controller(root: Path, *, authorize) -> bool:
    """Fence only an exact dead updater; preserve its generation and acceptance.

    The original supervisor supplies its live parent/OS binding. Holding the
    existing controller lock excludes a concurrent takeover. Missing or living
    ownership is not proof of death and never permits this transition.
    """
    descriptor = None
    try:
        descriptor = state._open_locked_file(state.lifecycle_paths(root).controller_lock_path,
            nonblocking=True)
        authority = state.inspect_state(root)
        operation = authority.active_operation
        if (operation is None or operation.operation_type is not state.OperationType.UPDATE
                or operation.owner_kind is not state.OwnerKind.UPDATER
                or operation.phase not in {state.LifecyclePhase.RESERVED,
                    state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING}):
            return False
        lease = state.read_controller_lease(root)
        if (lease is None or lease.operation_id != operation.operation_id
                or lease.generation != authority.generation or lease.owner_kind is not state.OwnerKind.UPDATER
                or state.controller_lease_is_current(lease)):
            return False
        try:
            observed, journal, bound = authorize(operation.operation_id, authority.generation)
        except (ControlError, update_controller.UpdateControllerError, OSError):
            return False
        if (observed != authority or bound.parent.original_generation >= authority.generation
                or state.inspect_state(root) != authority or state.read_controller_lease(root) != lease):
            return False
        state.transition_operation_with_credential(root, operation_id=operation.operation_id,
            generation=authority.generation, expected_phase=operation.phase,
            next_phase=state.LifecyclePhase.RECOVERY_REQUIRED)
        try:
            update_controller.reconcile_operator_journal_recovery_required(root,
                operation_id=operation.operation_id, generation=authority.generation,
                current_phase=state.LifecyclePhase.RECOVERY_REQUIRED,
                pre_cutover_baseline_rejected=operation.phase is state.LifecyclePhase.RESERVED)
        except (update_controller.UpdateControllerError, state.LifecycleError, OSError):
            # Canonical fencing succeeded. Stop owned children even if the
            # subordinate journal could not publish; retain the gap for review.
            pass
        return True
    except state.LifecycleConflictError:
        # The dying owner's shell can fence the same operation first. Let the
        # caller re-evaluate its exact RR/quiescent proof rather than exiting
        # the supervisor because this competing CAS already changed the phase.
        return False
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)


def failed_controller_is_quiescent(root: Path, *, authorize) -> bool:
    """Permit only stopping owned children after canonical failure is fenced.

    A caught error can release its lease before the supervisor observes it.
    Exact RR authority plus a free lock and positively empty fixed unit is
    sufficient for child shutdown, never for a new transfer or runtime start.
    No live recovery permission is needed to stop an already fenced runtime.
    """
    from .control.administration import operation_unit
    from .control.stop import _require_empty
    descriptor = None
    try:
        descriptor = state._open_locked_file(state.lifecycle_paths(root).controller_lock_path, nonblocking=True)
        authority = state.inspect_state(root)
        operation = authority.active_operation
        if (operation is None or operation.operation_type is not state.OperationType.UPDATE
                or operation.owner_kind is not state.OwnerKind.UPDATER
                or operation.phase is not state.LifecyclePhase.RECOVERY_REQUIRED):
            return False
        lease = state.read_controller_lease(root)
        observed, journal, bound = authorize(operation.operation_id, authority.generation)
        if lease is not None and (lease.operation_id != operation.operation_id
                or lease.owner_kind is not state.OwnerKind.UPDATER
                or lease.generation not in {authority.generation, journal['lifecycle_generation']}
                or state.controller_lease_is_current(lease)):
            return False
        actor = operation_unit(operation.operation_id)
        if (observed != authority or actor['ActiveState'] not in {'inactive', 'failed'}
                or int(actor['MainPID']) != 0):
            return False
        _require_empty('streamhome-operation@'+operation.operation_id+'.service')
        return (state.inspect_state(root) == authority and state.read_controller_lease(root) == lease
            and authorize(operation.operation_id, authority.generation) == (observed, journal, bound)
            and operation_unit(operation.operation_id) == actor)
    except (state.ControllerLockError, ControlError, update_controller.UpdateControllerError, OSError, ValueError):
        return False
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)


def require_initial_controller(root: Path, lease: state.ControllerLeaseHandle):
    """Validate the original fixed-unit preflight; this is not recovery consent."""
    from .control.administration import operation_unit
    from .control.privileged import unit_snapshot
    from .control.processes import controller_peer
    from .control.tickets import read_stop_epoch
    lease.require_current()
    if lease.paths.root != root or lease.lease.owner_kind is not state.OwnerKind.UPDATER:
        raise ControlError('control_controller_changed')
    authority, journal, bound = bound_journal(root, lease.lease.operation_id)
    parent = bound.parent
    active = read_active_execution(root)
    if (authority.generation != parent.original_generation + 1
            or authority.active_operation.phase is not state.LifecyclePhase.RESERVED
            or journal['phase'] not in {'preflight', 'waiting_for_idle'}
            or journal['runtime_committed'] or bound.accepted is not None
            or authority.generation != lease.lease.generation or lease.lease.pid != os.getpid()
            or active.installation_id != parent.installation_id or active.execution != parent.execution
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != parent.boot_id
            or read_stop_epoch() != parent.stop_epoch):
        raise ControlError('control_parent_binding_changed')
    controller_peer(os.getpid(), parent.operation_id)
    actor = operation_unit(parent.operation_id)
    if (actor['ActiveState'] != 'active' or int(actor['MainPID']) != os.getpid()
            or not os.environ.get('INVOCATION_ID') or actor['InvocationID'] != os.environ['INVOCATION_ID']):
        raise ControlError('control_controller_changed')
    supervisor = unit_snapshot()
    if (supervisor['ActiveState'] != 'active' or int(supervisor['MainPID']) != parent.supervisor_pid
            or supervisor['InvocationID'] != parent.supervisor_invocation_id
            or state._process_start_identity(parent.supervisor_pid) != parent.supervisor_start_identity):
        raise ControlError('control_parent_binding_changed')
    lease.require_current()
    return authority, journal, bound


def _process_record(pid: int) -> dict:
    from .control.administration_lock import process_identity
    from .control.processes import process_cgroup
    process = Path('/proc')/str(pid)
    start, status = process_identity(pid)
    fields = (process/'stat').read_text().rsplit(')', 1)[1].split()
    with (process/'cmdline').open('rb') as handle:
        arguments = handle.read(8193)
    result = {'pid': pid, 'uid': process.stat().st_uid, 'start': start, 'status': status,
        'parent': int(fields[1]), 'cgroup': process_cgroup(pid),
        'arguments': arguments.split(b'\0'), 'executable': os.readlink(process/'exe')}
    repeated_start, repeated_status = process_identity(pid)
    if (len(arguments) > 8192 or fields[19] != start or repeated_start != start
            or status in {'Z', 'X'} or repeated_status in {'Z', 'X'}):
        raise ControlError('control_process_changed')
    result['status'] = repeated_status
    return result


def require_owner_peer(root: Path, lease: state.ControllerLease, peer_pid: int):
    """Project the original operation-unit owner without acquiring authority."""
    from .control.administration import operation_unit
    from .control.tickets import read_stop_epoch
    if (type(peer_pid) is not int or peer_pid != lease.pid or peer_pid <= 1
            or lease.owner_kind is not state.OwnerKind.UPDATER
            or state.read_controller_lease(root) != lease or not state.controller_lease_is_current(lease)):
        raise ControlError('control_controller_changed')
    authority, journal, bound = bound_journal(root, lease.operation_id)
    if authority.generation != lease.generation:
        raise ControlError('control_parent_binding_changed')
    from .update_recovery import require_controller_permission
    permission = require_controller_permission(root, journal, bound, authority.generation)
    epoch = permission['stop_epoch'] if permission else bound.parent.stop_epoch
    boot = permission['boot_id'] if permission else bound.parent.boot_id
    if read_stop_epoch() != epoch or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot:
        raise ControlError('control_parent_binding_changed')
    actor = operation_unit(lease.operation_id)
    owner = _process_record(peer_pid)
    expected = [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision',
        b'--operation', lease.operation_id.encode(), b'']
    if (actor['ActiveState'] != 'active' or int(actor['MainPID']) != peer_pid or not actor['InvocationID']
            or owner['pid'] != peer_pid or owner['uid'] != os.geteuid()
            or owner['cgroup'] != '/system.slice/streamhome-operation@'+lease.operation_id+'.service'
            or owner['start'] != lease.process_start_identity or owner['status'] in {'Z', 'X'}
            or owner['executable'] != os.path.realpath('/usr/bin/python3') or owner['arguments'] != expected):
        raise ControlError('control_controller_forbidden')
    if (state.read_controller_lease(root) != lease or not state.controller_lease_is_current(lease)
            or bound_journal(root, lease.operation_id) != (authority, journal, bound)
            or read_stop_epoch() != epoch):
        raise ControlError('control_controller_changed')
    require_controller_permission(root, journal, bound, authority.generation)
    return authority, journal, bound


def require_delegate_peer(root: Path, lease: state.ControllerLease, helper_pid: int,
                          *, expected_invocation: str | None = None):
    """Read-only kernel/canonical projection for a known local IPC peer.

    This neither obtains a capability nor completes a controller lock check.
    The worker's proof additionally validates capability under the state lock;
    the owning supervisor combines this with its own original parent custody.
    """
    from .control.administration import operation_unit
    from .control.tickets import read_stop_epoch
    if type(helper_pid) is not int or helper_pid <= 1:
        raise ControlError('control_controller_forbidden')
    current = state.read_controller_lease(root)
    if (current != lease or lease.owner_kind is not state.OwnerKind.UPDATER
            or not state.controller_lease_is_current(lease)):
        raise ControlError('control_controller_changed')
    authority, journal, bound = bound_journal(root, lease.operation_id)
    if authority.generation != lease.generation:
        raise ControlError('control_parent_binding_changed')
    from .update_recovery import require_controller_permission
    permission = require_controller_permission(root, journal, bound, authority.generation)
    if permission is not None and permission['action'] in {'safe_abort', 'repair_bound_environment','repair_bound_control'}:
        raise ControlError('control_recovery_action_unavailable')
    epoch = permission['stop_epoch'] if permission else bound.parent.stop_epoch
    boot = permission['boot_id'] if permission else bound.parent.boot_id
    if read_stop_epoch() != epoch or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot:
        raise ControlError('control_parent_binding_changed')
    group = '/system.slice/streamhome-operation@'+lease.operation_id+'.service'
    try:
        helper = _process_record(helper_pid)
        shell = _process_record(helper['parent'])
        owner = _process_record(lease.pid)
    except (OSError, ValueError, IndexError) as exc:
        raise ControlError('control_controller_changed') from exc
    unit = operation_unit(lease.operation_id)
    if (unit['ActiveState'] != 'active' or int(unit['MainPID']) != owner['pid']
            or not unit['InvocationID'] or expected_invocation is not None and unit['InvocationID'] != expected_invocation
            or owner['start'] != lease.process_start_identity
            or any(process['uid'] != os.geteuid() or process['cgroup'] != group
                or process['status'] in {'Z', 'X'} for process in (helper, shell, owner))
            or helper['pid'] != helper_pid or owner['pid'] != lease.pid
            or len({process['pid'] for process in (helper, shell, owner)}) != 3
            or shell['parent'] != owner['pid'] or type(journal.get('controller_pid')) is not int
            or journal['controller_pid'] != shell['pid']
            or journal.get('controller_start_ticks') != shell['start']):
        raise ControlError('control_controller_forbidden')
    expected_owner = [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision',
        b'--operation', lease.operation_id.encode(), b'']
    expected_script = layout_for_root(root).durable_runtime/('update-controller.'+lease.operation_id+'.sh')
    if authority.generation == bound.parent.original_generation+1:
        expected_tail = [os.fsencode(expected_script), b'--manual-execute', journal['target_commit'].encode(),
            journal['previous_commit'].encode(), journal['update_branch'].encode(), b'true', os.fsencode(root), b'']
    else:
        expected_tail = [os.fsencode(expected_script), b'--recover-interrupted', os.fsencode(root), b'']
    system_python = os.path.realpath('/usr/bin/python3')
    if (owner['arguments'] != expected_owner or owner['executable'] != system_python
            or helper['executable'] != system_python or shell['executable'] != '/usr/bin/bash'
            or shell['arguments'][:1] not in ([b'bash'], [b'/usr/bin/bash'], [b'/bin/bash'])
            or shell['arguments'][1:] != expected_tail):
        raise ControlError('control_controller_forbidden')
    if (state.read_controller_lease(root) != lease or not state.controller_lease_is_current(lease)
            or bound_journal(root, lease.operation_id) != (authority, journal, bound)
            or read_stop_epoch() != epoch):
        raise ControlError('control_controller_changed')
    require_controller_permission(root, journal, bound, authority.generation)
    return authority, journal, bound


@dataclass(frozen=True)
class UpdateDelegate:
    """Narrow child proof; it cannot release or acquire its owner's lock."""
    paths: state.LifecyclePaths
    lease: state.ControllerLease

    def require_current(self) -> None:
        authority, _, _ = require_delegate_peer(self.paths.root, self.lease, os.getpid(),
            expected_invocation=os.environ.get('INVOCATION_ID', ''))
        update_controller.validate_inherited_controller(self.paths.root,
            operation_id=self.lease.operation_id, generation=self.lease.generation,
            expected_phase=authority.active_operation.phase.value, supervisor_pid=self.lease.pid)
        if state.read_controller_lease(self.paths.root) != self.lease:
            raise ControlError('control_controller_changed')


def delegated_controller(root: Path, operation_id: str, generation: int) -> UpdateDelegate:
    lease = state.read_controller_lease(root)
    if (lease is None or lease.operation_id != operation_id or lease.generation != generation
            or lease.owner_kind is not state.OwnerKind.UPDATER):
        raise ControlError('control_controller_changed')
    proof = UpdateDelegate(state.lifecycle_paths(root), lease)
    proof.require_current()
    return proof


def quiescent_delegate(root: Path, lease: state.ControllerLease, *, helper_pid: int | None = None):
    """Name only the registered Bash and its fixed protected UPDATE worker.

    This is a read-only identity projection for stopped-runtime inspections.
    It never exempts arbitrary descendants or another service-UID process.
    """
    authority, journal, bound = require_owner_peer(root, lease, lease.pid)
    shell_pid = journal.get('controller_pid')
    if type(shell_pid) is not int or shell_pid <= 1:
        raise ControlError('control_controller_changed')
    if helper_pid is None:
        try:
            candidates = [int(value) for value in Path(
                f'/proc/{shell_pid}/task/{shell_pid}/children').read_text().split()]
        except (OSError, ValueError) as exc:
            raise ControlError('control_controller_changed') from exc
    else:
        candidates = [helper_pid]
    selected = []
    for pid in candidates:
        require_delegate_peer(root, lease, pid)
        process = _process_record(pid)
        expected = [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision',
            b'--worker', b'update-runtime', lease.operation_id.encode(), b'']
        if process['arguments'] != expected:
            raise ControlError('control_foreign_service_process')
        selected.append((pid, process['start']))
    if len(selected) != 1:
        raise ControlError('control_foreign_service_process')
    shell = _process_record(shell_pid)
    if (bound_journal(root, lease.operation_id) != (authority, journal, bound)
            or require_owner_peer(root, lease, lease.pid) != (authority, journal, bound)):
        raise ControlError('control_controller_changed')
    return shell_pid, shell['start'], *selected[0]


def supervisor_command(root: Path, proof: UpdateDelegate, action: str, *, authority_locked=False) -> dict:
    """Request only fixed internal child actions; never start or restart an OS unit."""
    from .control.privileged import unit_snapshot
    from .control.rpc import request
    from .control.notification import STARTUP_RECOVERY_SECONDS
    if action not in {'update_stop_children', 'update_assert_stopped', 'update_maintenance',
            'update_start_candidate', 'update_start_previous', 'update_transport_readiness'}:
        raise ControlError('control_action_unsupported')
    if authority_locked:
        # The link publisher already holds exact credential-backed state. Its
        # callback must not reacquire that lock in this process or in the Sup.
        require_delegate_peer(root, proof.lease, os.getpid(),
            expected_invocation=os.environ.get('INVOCATION_ID', ''))
    else:
        proof.require_current()
    authority, _, bound = bound_journal(root, proof.lease.operation_id)
    if authority.generation != proof.lease.generation:
        raise ControlError('control_controller_changed')
    parent = bound.parent
    unit = unit_snapshot()
    from .control.update_resumption_helpers import offline, wait
    if action in {'update_stop_children', 'update_assert_stopped', 'update_maintenance', 'update_transport_readiness'} and offline(root, proof):
        return ({'maintenance': True} if action == 'update_maintenance' else
            {'ready': False} if action == 'update_transport_readiness' else {'stopped': True})
    if action in {'update_start_candidate', 'update_start_previous'}:
        wait(root, proof)
        unit = unit_snapshot()
    if unit['InvocationID'] != parent.supervisor_invocation_id:
        from .control.update_resumption_runtime import require_binding
        enrolled = require_binding(root, unit['InvocationID'],
            control_manifest_sha256=parent.execution.control_manifest_sha256)
        return request(layout_for_root(root).runtime/'supervisor.sock', expected_uid=os.geteuid(),
            expected_pid=enrolled.pid, installation_id=parent.installation_id,
            invocation=enrolled.invocation, action=action,
            arguments={'operation_id': parent.operation_id, 'generation': authority.generation},
            timeout=STARTUP_RECOVERY_SECONDS+30 if action in {'update_start_candidate', 'update_start_previous'} else 60)
    if (unit['ActiveState'] != 'active' or int(unit['MainPID']) != parent.supervisor_pid
            or unit['InvocationID'] != parent.supervisor_invocation_id
            or state._process_start_identity(parent.supervisor_pid) != parent.supervisor_start_identity):
        raise ControlError('control_parent_binding_changed')
    return request(layout_for_root(root).runtime/'supervisor.sock', expected_uid=os.geteuid(),
        expected_pid=parent.supervisor_pid, installation_id=parent.installation_id,
        invocation=parent.supervisor_invocation_id, action=action,
        arguments={'operation_id': parent.operation_id, 'generation': authority.generation},
        timeout=STARTUP_RECOVERY_SECONDS+30 if action in {'update_start_candidate', 'update_start_previous'} else 60)


def runtime_action(root: Path, proof: UpdateDelegate, action: str) -> dict:
    proof.require_current()
    if action in {'create_checkpoint', 'restore_checkpoint', 'cleanup_checkpoint'}:
        from . import update_checkpoint
        return {'create_checkpoint': update_checkpoint.create, 'restore_checkpoint': update_checkpoint.restore,
            'cleanup_checkpoint': update_checkpoint.cleanup}[action](root, proof)
    if action == 'prepared':
        authority, journal, bound = bound_journal(root, proof.lease.operation_id)
        if (authority.active_operation.phase is not state.LifecyclePhase.RESERVED
                or journal['phase'] != 'preflight' or bound.candidate is None):
            raise ControlError('control_authority_changed')
        layout = layout_for_root(root)
        source = preparation_source(root, layout.cache/'update-preparation'/bound.parent.operation_id/'checkout')
        verify_runtime(root, bound.candidate, source_root=source)
        proof.require_current()
        if bound_journal(root, proof.lease.operation_id) != (authority, journal, bound):
            raise ControlError('control_parent_binding_changed')
        return {'prepared': True}
    if action == 'assert_rollback':
        authority, journal, bound = bound_journal(root, proof.lease.operation_id)
        if (bound.accepted is not None or journal['phase'] != 'rolling_back'
                or authority.active_operation.phase not in {state.LifecyclePhase.EXCLUSIVE, state.LifecyclePhase.VERIFYING}):
            raise ControlError('control_authority_changed')
        if supervisor_command(root, proof, 'update_assert_stopped') != {'stopped': True}:
            raise ControlError('control_children_busy')
        from .update_checkpoint import require_checkpoint
        require_checkpoint(root, bound, checkpoint=proof.require_current)
        proof.require_current()
        if bound_journal(root, proof.lease.operation_id) != (authority, journal, bound):
            raise ControlError('control_parent_binding_changed')
        return {'rollback_allowed': True}
    if action == 'verify_previous':
        _, _, bound = bound_journal(root, proof.lease.operation_id)
        verify_runtime(root, bound.parent.execution)
        proof.require_current()
        return {'verified': True}
    if action == 'start':
        _, journal, bound = bound_journal(root, proof.lease.operation_id)
        if journal['phase'] == 'starting':
            action = 'start_candidate'
        elif journal['phase'] in {'failed', 'rolled_back'}:
            action = 'start_previous'
        elif journal['phase'] == 'recovering' and (bound.accepted is not None
                or journal['current_commit'] == journal['previous_commit']):
            action = 'start_previous' if bound.accepted in (None, bound.parent.execution) else 'start_candidate'
        else:
            raise ControlError('control_authority_changed')
    if action == 'readiness':
        result = supervisor_command(root, proof, 'update_transport_readiness')
        if not isinstance(result, dict) or set(result) != {'ready'} or type(result['ready']) is not bool:
            raise ControlError('control_reply_invalid')
        return result
    if action in {'start_candidate', 'start_previous'}:
        from .update_checkpoint import require_launch_database
        _, _, bound = bound_journal(root, proof.lease.operation_id)
        require_launch_database(root, bound)
        proof.require_current()
        target = 'candidate' if action == 'start_candidate' else 'previous'
        selected = select_verification(root, proof, target)
        result = supervisor_command(root, proof, 'update_'+action)
        if (not isinstance(result, dict) or set(result) != {'launched', 'build_id'}
                or result['launched'] is not True or result['build_id'] != selected.application_build_id):
            raise ControlError('control_reply_invalid')
        return result
    if action in {'bind_candidate', 'bind_previous'}:
        from .runtime_links import bind_update_runtime
        def stopped():
            if supervisor_command(root, proof, 'update_assert_stopped', authority_locked=True) != {'stopped': True}:
                raise ControlError('control_children_busy')
        target = 'candidate' if action == 'bind_candidate' else 'previous'
        bind_update_runtime(root, proof=proof, target=target, assert_children_stopped=stopped)
        return {'bound': True, 'target': target, 'generation': proof.lease.generation}
    mapping = {'stop': ('update_stop_children', {'stopped': True}),
        'assert_stopped': ('update_assert_stopped', {'stopped': True}),
        'maintenance': ('update_maintenance', {'maintenance': True})}
    if action not in mapping:
        raise ControlError('control_action_unsupported')
    command, expected = mapping[action]
    if supervisor_command(root, proof, command) != expected:
        raise ControlError('control_reply_invalid')
    return expected



def select_verification(root: Path, proof: UpdateDelegate, target: str):
    """Bind one stopped guarded launch in the original UPDATE journal.

    Only the pre-acceptance candidate-to-previous rollback may change selection.
    An interrupted launch retains its selection; it never authorizes acceptance.
    """
    from .management_journal import execution_tuple_sha256
    from .runtime_links import verify_links
    if target not in {'candidate', 'previous'}:
        raise ControlError('control_action_unsupported')
    proof.require_current()
    authority, original, bound = bound_journal(root, proof.lease.operation_id)
    selected = bound.candidate if target == 'candidate' else bound.parent.execution
    if (selected is None or authority.active_operation.phase is not state.LifecyclePhase.VERIFYING
            or bound.accepted is not None and bound.accepted != selected
            or original['phase'] not in ({'starting', 'recovering'} if target == 'candidate'
                else {'failed', 'rolled_back', 'recovering'})):
        raise ControlError('control_authority_changed')
    verify_runtime(root, selected)
    verify_links(root, selected)
    updated = {**original, 'managed_verification_execution_sha256': execution_tuple_sha256(selected)}
    update_execution.require_transition(original, updated)
    path, lock_path, _ = update_controller._update_paths(root, proof.lease.operation_id, proof.lease.generation)
    descriptor = state._open_locked_file(lock_path)
    try:
        if update_controller.read_update_journal(path) != original:
            raise ControlError('control_parent_binding_changed')
        with state.hold_operation_with_credential(root, operation_id=proof.lease.operation_id,
                generation=proof.lease.generation, expected_phase=state.LifecyclePhase.VERIFYING,
                expected_operation_type=state.OperationType.UPDATE) as current:
            if current != authority:
                raise ControlError('control_authority_changed')
            if supervisor_command(root, proof, 'update_assert_stopped', authority_locked=True) != {'stopped': True}:
                raise ControlError('control_children_busy')
            update_controller._validate_update_journal_semantics(updated, authority=current)
            from .runtime_build import canonical_bytes
            update_controller._private_atomic_write(path, canonical_bytes(updated))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)
    return selected


def verification_execution(root: Path, environ):
    """Pre-import selection for a supervisor-owned managed guarded backend."""
    from .control.privileged import unit_snapshot
    from .management_journal import execution_tuple_sha256
    from .control.tickets import read_stop_epoch
    authority, journal, bound = bound_journal(root, environ.get('STREAMHOME_LIFECYCLE_OPERATION_ID', ''))
    parent = bound.parent
    lease = state.read_controller_lease(root)
    unit = unit_snapshot()
    from .update_recovery import require_controller_permission
    permission = require_controller_permission(root, journal, bound, authority.generation)
    if permission is not None and permission['action'] in {'repair_bound_environment','repair_bound_control'}:
        raise ControlError('control_recovery_action_unavailable')
    if permission is not None and permission['schema_version'] == 2:
        from .control.update_resumption_runtime import require_binding
        enrolled = require_binding(root, unit['InvocationID'],
            control_manifest_sha256=parent.execution.control_manifest_sha256)
        if (authority.active_operation.phase is not state.LifecyclePhase.VERIFYING
                or bound.verification is None or execution_tuple_sha256(bound.verification) != enrolled.execution_sha256
                or environ.get('STREAMHOME_LIFECYCLE_GENERATION') != str(authority.generation)
                or environ.get('INVOCATION_ID') != enrolled.invocation or os.getppid() != enrolled.pid
                or journal['phase'] not in {'starting', 'recovering', 'failed', 'rolled_back', 'succeeded'}):
            raise ControlError('control_parent_binding_changed')
        from .update_checkpoint import require_launch_database
        require_launch_database(root, bound)
        return bound.verification
    if (authority.active_operation.phase is not state.LifecyclePhase.VERIFYING
            or bound.verification is None or lease is None
            or lease.operation_id != parent.operation_id or lease.generation != authority.generation
            or lease.owner_kind is not state.OwnerKind.UPDATER or not state.controller_lease_is_current(lease)
            or environ.get('STREAMHOME_LIFECYCLE_GENERATION') != str(authority.generation)
            or environ.get('INVOCATION_ID') != parent.supervisor_invocation_id
            or os.getppid() != parent.supervisor_pid
            or unit['ActiveState'] != 'active' or int(unit['MainPID']) != parent.supervisor_pid
            or unit['InvocationID'] != parent.supervisor_invocation_id
            or state._process_start_identity(parent.supervisor_pid) != parent.supervisor_start_identity
            or read_stop_epoch() != parent.stop_epoch
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != parent.boot_id
            or journal['phase'] not in {'starting', 'recovering', 'failed', 'rolled_back', 'succeeded'}):
        raise ControlError('control_parent_binding_changed')
    if bound_journal(root, parent.operation_id) != (authority, journal, bound):
        raise ControlError('control_authority_changed')
    from .update_checkpoint import require_launch_database
    require_launch_database(root, bound)
    return bound.verification


def retain_completed_from_delegate(root: Path, proof: UpdateDelegate) -> dict:
    """Finalize existing accepted recovery status before retaining its evidence."""
    proof.require_current()
    authority, journal, bound = bound_journal(root, proof.lease.operation_id)
    if bound.accepted is not None and runtime_action(root, proof, 'readiness') != {'ready': True}:
        raise ControlError('control_runtime_not_ready')
    if journal['phase'] == 'recovering' and bound.accepted is not None:
        previous = bound.accepted == bound.parent.execution
        update_controller.mutate_update_journal(root, operation_id=bound.parent.operation_id,
            generation=authority.generation, expected_lifecycle_phase=state.LifecyclePhase.VERIFYING,
            expected_detail_phases=frozenset({'recovering'}), changes={
                'phase': 'rolled_back' if previous else 'succeeded',
                'message': ('The previous accepted release completed mandatory recovery.' if previous
                    else 'The updated accepted release completed mandatory recovery.'),
                'error': 'update_rolled_back' if previous else '',
                'failed_target': journal['target_commit'] if previous else '',
                'current_commit': bound.accepted.application_commit})
    proof.require_current()
    from .update_history import retain_terminal
    return retain_terminal(root, proof)


def prepared_execution(previous, target_commit: str, built: PreparedRuntime):
    """Construct the exact target tuple while preserving active deployment/control."""
    runtime = Path('/opt/streamhome/runtime')
    return replace(previous, application_commit=target_commit, application_build_id=target_commit[:12],
        runtime_bundle_id=built.bundle_id, runtime_manifest_sha256=built.manifest_sha256,
        python_environment_id=built.python_id, python_executable=str(runtime/'python'/built.python_id/'venv/bin/python'),
        node_environment_id=built.node_id, node_executable=str(runtime/'node'/built.node_id/'bin/node'),
        web_bundle_id=built.web_id, web_dependency_target=str(runtime/'web'/built.web_id/'node_modules'),
        web_dist_target=str(runtime/'web'/built.web_id/'dist'), web_asset_manifest_sha256=built.web_sha256)


def prepare_checkout(root: Path, lease: state.ControllerLeaseHandle) -> Path:
    """Create an inactive exact target source; retain failed preparation evidence."""
    from .runtime_build import clean_environment, run_stage, source_inputs
    authority, original, bound = require_initial_controller(root, lease)
    if bound.candidate is not None:
        raise ControlError('control_update_candidate_already_bound')
    layout = layout_for_root(root)
    state._validate_directory(layout.cache, 'update preparation cache parent')
    base = layout.cache/'update-preparation'
    state._ensure_private_directory(base, 'update preparation cache')
    directory = base/bound.parent.operation_id
    # An existing attempt is evidence, never an implicit retry workspace.
    directory.mkdir(mode=0o700)
    source = directory/'checkout'
    home = directory/'git-home'
    home.mkdir(mode=0o700);(home/'tmp').mkdir(mode=0o700)
    environment = clean_environment(home)
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL='/dev/null', GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0')
    def checkpoint():
        current, journal, selected = require_initial_controller(root, lease)
        if current != authority or journal != original or selected != bound:
            raise ControlError('control_parent_binding_changed')
    git = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false']
    run_stage(git+['clone', '--shared', '--no-checkout', str(root), str(source)],
        cwd=directory, environment=environment, log=directory/'clone.log', timeout=180, checkpoint=checkpoint)
    source.chmod(0o700)
    preparation_source(root, source)
    run_stage(git+['checkout', '--detach', original['target_commit']], cwd=source,
        environment=environment, log=directory/'checkout.log', timeout=60, checkpoint=checkpoint)
    run_stage(git+['merge-base', '--is-ancestor', original['previous_commit'], original['target_commit']],
        cwd=source, environment=environment, log=directory/'ancestry.log', timeout=30, checkpoint=checkpoint)
    source_inputs(source, original['target_commit'])
    checkpoint()
    return source


def prepare_candidate(root: Path, lease: state.ControllerLeaseHandle, *, stage=None):
    authority, original, bound = require_initial_controller(root, lease)
    if bound.candidate is not None:
        raise ControlError('control_update_candidate_already_bound')
    operation = bound.parent.operation_id
    layout = layout_for_root(root)
    source = preparation_source(root, layout.cache/'update-preparation'/operation/'checkout')
    full_check_at = 0.0
    def checkpoint(*, force=False):
        nonlocal full_check_at
        from .control.tickets import read_stop_epoch
        lease.require_current()
        if read_stop_epoch() != bound.parent.stop_epoch:
            raise ControlError('control_operator_stopped')
        if not force and time.monotonic() < full_check_at:
            return
        current, journal, selected = require_initial_controller(root, lease)
        if current != authority or journal != original or selected != bound:
            raise ControlError('control_parent_binding_changed')
        full_check_at = time.monotonic() + 1
    checkpoint(force=True)
    built = prepare_runtime(root, build_id=original['target_commit'][:12],
        application_commit=original['target_commit'], source_root=source,
        checkpoint=checkpoint, stage=stage)
    candidate = prepared_execution(bound.parent.execution, original['target_commit'], built)
    updated = {**original, 'managed_candidate': execution_tuple_payload(candidate)}
    update_execution.require_transition(original, updated)
    verify_runtime(root, candidate, source_root=source)
    checkpoint(force=True)
    path, lock_path, _ = update_controller._update_paths(root, operation, authority.generation)
    descriptor = state._open_locked_file(lock_path)
    try:
        if update_controller.read_update_journal(path) != original:
            raise ControlError('control_parent_binding_changed')
        with state.hold_operation_with_credential(root, operation_id=operation,
                generation=authority.generation, expected_phase=state.LifecyclePhase.RESERVED,
                expected_operation_type=state.OperationType.UPDATE) as current:
            if current != authority:
                raise ControlError('control_authority_changed')
            update_controller._validate_update_journal_semantics(updated, authority=current)
            from .runtime_build import canonical_bytes
            update_controller._private_atomic_write(path, canonical_bytes(updated))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)
    return candidate


def read_commit_token(root: Path, operation_id: str) -> str:
    """Read the existing engine's finite private token without following aliases."""
    import stat
    import re
    # _update_paths validates the operation identity before constructing paths.
    directory = update_controller._update_paths(root, operation_id, 1)[0].parent
    state._validate_directory(directory, 'update commit token directory')
    path = directory/('update-commit.'+operation_id+'.token')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o7177 or before.st_nlink != 1 or before.st_size > 128):
            raise ControlError('control_update_token_untrusted')
        raw = handle.read(129)
        after, linked = os.fstat(handle.fileno()), path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_uid,
            item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or identity(before) != identity(linked):
            raise ControlError('control_update_token_changed')
    if not re.fullmatch(rb'[A-Za-z0-9_-]{43}\n?', raw):
        raise ControlError('control_update_token_untrusted')
    return raw.decode('ascii').rstrip('\n')


def _require_acceptance_ready(root: Path, operation_id: str, generation: int) -> None:
    from .bootstrap import (StartupMode, require_startup_context, require_current_guarded_authority,
        current_readiness_projection, get_startup_readiness)
    context = require_startup_context()
    if (context.installation_root != root or context.mode is not StartupMode.UPDATE_VERIFY
            or context.operation_id != operation_id or context.generation != generation):
        raise ControlError('control_authority_changed')
    require_current_guarded_authority(context)
    if current_readiness_projection(get_startup_readiness()).get('guardedReady') is not True:
        raise ControlError('control_guarded_recovery_incomplete')


def accept_verification(root: Path, *, operation_id: str, generation: int,
                        expected_detail_phase: str) -> dict:
    """Accept before publishing the selected runtime; a crash can only go forward.

    The HTTP route authenticates the existing one-use loopback commit token.
    This adapter independently checks the actual guarded backend, canonical
    authority and original journal. It never accepts on the controller's behalf.
    """
    from .execution_state import ExecutionRecord
    from .management_journal import execution_tuple_sha256
    from .runtime_build import canonical_bytes
    _require_acceptance_ready(root, operation_id, generation)
    selected = verification_execution(root, os.environ)
    authority, original, bound = bound_journal(root, operation_id)
    if (authority.generation != generation or original['phase'] != expected_detail_phase
            or bound.verification != selected or bound.accepted is not None and bound.accepted != selected):
        raise ControlError('control_authority_changed')
    updated = {**original, 'runtime_committed': True,
        'runtime_committed_at': original.get('runtime_committed_at') if bound.accepted else time.time(),
        'managed_accepted_execution_sha256': execution_tuple_sha256(selected),
        'current_commit': selected.application_commit}
    update_execution.require_transition(original, updated)
    path, lock_path, _ = update_controller._update_paths(root, operation_id, generation)
    descriptor = state._open_locked_file(lock_path)
    try:
        if update_controller.read_update_journal(path) != original:
            raise ControlError('control_parent_binding_changed')
        with state.hold_operation_with_credential(root, operation_id=operation_id, generation=generation,
                expected_phase=state.LifecyclePhase.VERIFYING,
                expected_operation_type=state.OperationType.UPDATE) as current:
            if current != authority or verification_execution(root, os.environ) != selected:
                raise ControlError('control_authority_changed')
            active = read_active_execution(root)
            replacement = ExecutionRecord(bound.parent.installation_id, selected, operation_id, generation)
            from .update_recovery import digest, require_controller_permission
            permission = require_controller_permission(root, original, bound, generation)
            reviewed_source = (permission is not None
                and digest(active.payload()) == permission['active_record_sha256'])
            original_active = (active.execution == bound.parent.execution
                and active.generation < bound.parent.original_generation)
            if (active.installation_id != bound.parent.installation_id
                    or not (original_active or reviewed_source)
                        and not (bound.accepted is not None and active == replacement)):
                raise ControlError('control_parent_binding_changed')
            update_controller._validate_update_journal_semantics(updated, authority=current)
            # Publish durable irreversible evidence first. If pointer publication
            # fails, recovery must retain this exact selected runtime.
            update_controller._private_atomic_write(path, canonical_bytes(updated))
            state._atomic_write_json(layout_for_root(root).state/'active-runtime.json', replacement.payload())
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN);os.close(descriptor)
    return updated


def execution_context(root: Path, proof: UpdateDelegate) -> dict:
    """Project canonical script paths and the pinned active deployment as data.

    No .env parsing, guessed database paths, desired configuration activation or
    ambient interpreter discovery is part of managed execution.
    """
    from .configuration_store import ConfigurationStore
    proof.require_current()
    authority, journal, bound = bound_journal(root, proof.lease.operation_id)
    if bound.candidate is None:
        raise ControlError('control_update_candidate_unprepared')
    layout = layout_for_root(root)
    descriptor = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with ConfigurationStore(descriptor, bound.parent.installation_id) as store:
            manifest = store.inspect()
            if (manifest.active.revision != bound.parent.execution.deployment_revision
                    or manifest.active.revision in manifest.quarantined
                    or manifest.policy_revisions != bound.parent.execution.policy_revisions
                    or manifest.provider_revisions != bound.parent.execution.provider_revisions):
                raise ControlError('control_deployment_changed')
            config = store.read_revision(manifest.active).settings
    finally:
        os.close(descriptor)
    source = layout.cache/'update-preparation'/bound.parent.operation_id/'checkout'
    if bound.accepted is None:
        source = preparation_source(root, source)
    proof.require_current()
    if bound_journal(root, proof.lease.operation_id) != (authority, journal, bound):
        raise ControlError('control_parent_binding_changed')
    from .update_checkpoint import path_for
    return {'schema_version': 1, 'operation_id': bound.parent.operation_id, 'generation': authority.generation,
        'database_checkpoint': str(path_for(root, bound.parent.operation_id)),
        'recovery_commit': (bound.accepted or bound.candidate).application_commit,
        'recovery_target': 'previous' if bound.accepted == bound.parent.execution else 'candidate',
        'runtime_directory': str(layout.durable_runtime), 'database': str(layout.database),
        'update_log': str(layout.durable_runtime/'update.log'), 'staging_directory': str(source.parent),
        'previous_python': bound.parent.execution.python_executable,
        'candidate_python': bound.candidate.python_executable,
        'web_port': config.web_port, 'public_origin': config.public_origin}


def spawn_initial(root: Path, *, operation_id: str, generation: int, target_commit: str,
                  previous_commit: str, automatic: bool, install_mode: str, update_branch: str) -> update_controller.ManagedUpdateHandoff:
    """Ask the original supervisor for the one fixed operation-unit owner.

    Existing UPDATE credentials remain in their original private durable record;
    this path transports no capability, response gate or arbitrary executable.
    """
    from .control.rpc import request
    from .runtime_admission import require_runtime_admission
    from .control.reader_support import require_managed_update_reader
    from .update_continuation import reservation, require_live
    authority, journal, bound = bound_journal(root, operation_id)
    require_managed_update_reader(bound.parent.execution)
    admission = require_runtime_admission()
    pending = reservation(journal)
    if (type(automatic) is not bool or type(generation) is not int
            or generation != bound.parent.original_generation or generation != authority.generation
            or journal['phase'] != 'queued' or bound.candidate is not None
            or journal['target_commit'] != target_commit or journal['previous_commit'] != previous_commit
            or journal['automatic'] != automatic or journal['install_mode'] != install_mode
            or journal['update_branch'] != update_branch
            or admission._pending_update != pending or not admission.admitted()):
        raise ControlError('control_update_reservation_changed')
    if require_live(root, pending, admission.generation, admission.installation_id,
            admission.invocation, admission.execution) != authority:
        raise ControlError('control_update_reservation_changed')
    parent = bound.parent
    result = request(layout_for_root(root).runtime/'supervisor.sock', expected_uid=os.geteuid(),
        expected_pid=parent.supervisor_pid, installation_id=parent.installation_id,
        invocation=parent.supervisor_invocation_id, action='start_update_operation',
        arguments={'operation_id': operation_id, 'generation': generation}, timeout=30)
    if (not isinstance(result, dict) or set(result) != {'operation_id', 'generation', 'controller_pid'}
            or result['operation_id'] != operation_id or type(result['generation']) is not int
            or result['generation'] != generation+1 or type(result['controller_pid']) is not int
            or result['controller_pid'] <= 1):
        raise ControlError('control_reply_invalid')
    lease = state.read_controller_lease(root)
    if (lease is None or lease.pid != result['controller_pid'] or lease.generation != generation+1
            or require_owner_peer(root, lease, lease.pid)[2].parent != parent):
        raise ControlError('control_controller_changed')
    return update_controller.ManagedUpdateHandoff(operation_id, result['generation'], result['controller_pid'])


def require_operation_entry(operation_id: str) -> None:
    """Require the fixed unit's actual MainPID and protected OS interpreter/argv."""
    from .control.administration import operation_unit
    from .control.processes import controller_peer
    controller_peer(os.getpid(), operation_id)
    actor = operation_unit(operation_id)
    process = _process_record(os.getpid())
    if (actor['ActiveState'] != 'active' or int(actor['MainPID']) != os.getpid()
            or not os.environ.get('INVOCATION_ID') or actor['InvocationID'] != os.environ['INVOCATION_ID']
            or process['uid'] != os.geteuid() or process['status'] in {'Z', 'X'}
            or process['arguments'] != [b'/usr/bin/python3', b'-I', b'/usr/local/libexec/streamhome/provision',
                b'--operation', operation_id.encode(), b'']
            or process['executable'] != os.path.realpath('/usr/bin/python3')):
        raise ControlError('control_controller_forbidden')


def require_initial_dispatch(root: Path, operation_id: str, *, control_manifest_sha256: str):
    """Read-only original queued proof, also safe inside the transfer state lock."""
    from .control.privileged import unit_snapshot
    from .control.tickets import read_stop_epoch
    authority, journal, bound = bound_journal(root, operation_id)
    parent = bound.parent
    active = read_active_execution(root)
    if (authority.active_operation.phase is not state.LifecyclePhase.RESERVED
            or authority.generation != parent.original_generation or journal['phase'] != 'queued'
            or bound.candidate is not None or bound.accepted is not None
            or parent.execution.control_manifest_sha256 != control_manifest_sha256
            or active.installation_id != parent.installation_id or active.execution != parent.execution
            or active.generation >= parent.original_generation
            or read_stop_epoch() != parent.stop_epoch
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != parent.boot_id):
        raise ControlError('control_update_reservation_changed')
    require_operation_entry(operation_id)
    supervisor = unit_snapshot()
    if (supervisor['ActiveState'] != 'active' or int(supervisor['MainPID']) != parent.supervisor_pid
            or supervisor['InvocationID'] != parent.supervisor_invocation_id
            or state._process_start_identity(parent.supervisor_pid) != parent.supervisor_start_identity):
        raise ControlError('control_parent_binding_changed')
    if bound_journal(root, operation_id) != (authority, journal, bound) or read_stop_epoch() != parent.stop_epoch:
        raise ControlError('control_update_reservation_changed')
    return authority, journal, bound


def prepare_initial(root: Path, lease: state.ControllerLeaseHandle) -> None:
    """Prepare before the existing shell engine starts; preserve all failed inputs."""
    lease.require_current()
    authority, journal, bound = require_owner_peer(root, lease.lease, os.getpid())
    if (authority.active_operation.phase is not state.LifecyclePhase.RESERVED
            or journal['phase'] != 'queued' or bound.candidate is not None):
        raise ControlError('control_update_reservation_changed')
    update_controller.mutate_update_journal(root, operation_id=lease.lease.operation_id,
        generation=lease.lease.generation, expected_lifecycle_phase=state.LifecyclePhase.RESERVED,
        expected_detail_phases=frozenset({'queued'}), changes={'phase': 'preflight',
            'message': 'Preparing the exact managed runtime while the original service remains online.'})
    require_initial_controller(root, lease)
    source = prepare_checkout(root, lease)
    script = source/'scripts/runtime/update.sh'
    if script.is_symlink() or not script.is_file():
        raise ControlError('control_update_target_unsupported')
    with script.open('rb') as handle:
        prefix = handle.read(1024)
    if b'# StreamHome managed update engine: 1' not in prefix.splitlines()[:4]:
        raise ControlError('control_update_target_unsupported')
    prepare_candidate(root, lease)


def execute(root: Path, operation_id: str, *, control_manifest_sha256: str) -> int:
    """Initial fixed-unit adapter around the existing canonical UPDATE engine."""
    authority = state.inspect_state(root)
    operation = authority.active_operation
    if (operation is not None and operation.operation_id == operation_id
            and operation.operation_type is state.OperationType.UPDATE
            and operation.phase in {state.LifecyclePhase.RESERVED, state.LifecyclePhase.RECOVERY_REQUIRED}):
        from .control import update_grants
        try:
            update_grants.grant_path(operation_id, authority.generation+1).lstat()
        except FileNotFoundError:
            protected = False
        else:
            protected = True
        if protected or operation.phase is state.LifecyclePhase.RECOVERY_REQUIRED:
            if protected:
                grant = update_grants.read_grant(operation_id, authority.generation+1)
                if grant['schema_version']==4:
                    from .bound_control_parent_repair import BoundUpdateControlController
                    return BoundUpdateControlController(root,operation_id,
                        control_manifest_sha256=control_manifest_sha256).execute()
                if grant['schema_version'] == 3:
                    from .bound_update_repair import BoundUpdateEnvironmentController
                    return BoundUpdateEnvironmentController(root, operation_id,
                        control_manifest_sha256=control_manifest_sha256).execute()
            from .update_recovery import execute as recover_update
            return recover_update(root, operation_id, control_manifest_sha256=control_manifest_sha256)
    original = require_initial_dispatch(root, operation_id, control_manifest_sha256=control_manifest_sha256)
    authority, journal, bound = original
    from .control.reader_support import require_managed_update_reader
    require_managed_update_reader(bound.parent.execution)
    def pre_transfer():
        if require_initial_dispatch(root, operation_id,
                control_manifest_sha256=control_manifest_sha256) != original:
            raise ControlError('control_update_reservation_changed')
    return update_controller._run_controller(root, operation_id=operation_id,
        generation=authority.generation, target_commit=journal['target_commit'],
        previous_commit=journal['previous_commit'], automatic=journal['automatic'],
        install_mode=journal['install_mode'], update_branch=journal['update_branch'],
        acknowledgement_fd=None, recovery=False, pre_transfer_check=pre_transfer,
        managed_initial=True)


def retire_failed_preflight(root: Path, lease: state.ControllerLeaseHandle) -> bool:
    """Keep the original healthy service after proven pre-script preparation failure.

    This uses the original updater's terminal history and completion primitive;
    no runtime, database, configuration or boot-policy rollback occurs here.
    """
    from .runtime_links import verify_links
    from .control.rpc import request
    authority, journal, bound = require_initial_controller(root, lease)
    if (journal['phase'] != 'preflight' or journal.get('controller_pid') is not None
            or journal.get('web_artifacts_swapped') or journal.get('web_dependencies_swapped')
            or bound.accepted is not None or bound.verification is not None):
        raise ControlError('control_update_preflight_abort_unproven')
    update_controller.validate_pre_cutover_baseline(root, journal['previous_commit'])
    verify_runtime(root, bound.parent.execution)
    verify_links(root, bound.parent.execution)
    parent = bound.parent
    result = request(layout_for_root(root).runtime/'supervisor.sock', expected_uid=os.geteuid(),
        expected_pid=parent.supervisor_pid, installation_id=parent.installation_id,
        invocation=parent.supervisor_invocation_id, action='update_preflight_readiness',
        arguments={'operation_id': parent.operation_id, 'generation': authority.generation}, timeout=10)
    if not isinstance(result, dict) or set(result) != {'ready'} or result['ready'] is not True:
        raise ControlError('control_runtime_not_ready')
    if require_initial_controller(root, lease) != (authority, journal, bound):
        raise ControlError('control_update_preflight_abort_unproven')
    update_controller.mutate_update_journal(root, operation_id=parent.operation_id,
        generation=authority.generation, expected_lifecycle_phase=state.LifecyclePhase.RESERVED,
        expected_detail_phases=frozenset({'preflight'}), changes={'phase': 'failed',
            'message': 'Target preparation failed. The original verified service remains active.',
            'error': 'preflight_failed', 'failed_target': journal['target_commit'],
            'current_commit': journal['previous_commit']})
    from .update_history import retain_terminal
    retain_terminal(root, lease)
    state.complete_operation_with_credential(root, operation_id=parent.operation_id,
        generation=authority.generation, expected_phase=state.LifecyclePhase.RESERVED,
        expected_operation_type=state.OperationType.UPDATE)
    return True
