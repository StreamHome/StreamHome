"""Immutable managed execution captured by an existing UPDATE/RESTORE owner.

The record belongs inside the original parent's journal. It is evidence for that
parent's controller, never a competing claim or an inferred active execution for
an old unbound journal. Parsing alone authorizes no start, takeover or repair.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from . import management_journal as contract, state
from .execution_state import read_active_execution
from .layout import layout_for_root
from .control.rpc import ControlError


@dataclass(frozen=True)
class ParentExecution:
    installation_id: str
    operation_id: str
    operation_type: str
    original_generation: int
    execution: contract.ExecutionTuple
    supervisor_invocation_id: str
    supervisor_pid: int
    supervisor_start_identity: str
    boot_id: str
    stop_epoch: str

    def payload(self) -> dict:
        return {'schema_version': 1, **self.__dict__, 'execution': contract.execution_tuple_payload(self.execution)}


def parse_parent_execution(value: dict) -> ParentExecution:
    fields = {'schema_version', 'installation_id', 'operation_id', 'operation_type', 'original_generation',
              'execution', 'supervisor_invocation_id', 'supervisor_pid', 'supervisor_start_identity', 'boot_id', 'stop_epoch'}
    try:
        if (not isinstance(value, dict) or set(value) != fields
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not isinstance(value['operation_type'], str) or value['operation_type'] not in {'update', 'restore'}):
            raise ControlError('control_parent_binding_invalid')
        for name in ('installation_id', 'operation_id', 'boot_id'):
            contract._uuid(value[name], name)
        contract._integer(value['original_generation'], 1, 'parent generation')
        contract._integer(value['supervisor_pid'], 2, 'parent supervisor PID')
        if value['supervisor_pid'] >= 2 ** 31:
            raise ControlError('control_parent_binding_invalid')
        contract._text(value['supervisor_invocation_id'], contract._INVOCATION, 'parent supervisor invocation')
        if int(value['supervisor_invocation_id'], 16) == 0:
            raise ControlError('control_parent_binding_invalid')
        import re
        if (not isinstance(value['supervisor_start_identity'], str)
                or not re.fullmatch(r'[0-9]{1,20}', value['supervisor_start_identity'])):
            raise ControlError('control_parent_binding_invalid')
        contract._text(value['stop_epoch'], contract._SHA256, 'parent stop intent')
        execution = contract.parse_execution_tuple(value['execution'])
        return ParentExecution(**{name: execution if name == 'execution' else value[name]
                                  for name in fields if name != 'schema_version'})
    except (ValueError, TypeError, KeyError) as exc:
        raise ControlError('control_parent_binding_invalid') from exc


def capture(root: Path, *, operation_id: str, generation: int, operation_type: str) -> ParentExecution:
    """Capture only from the exact existing parent's still-running backend."""
    from .control.privileged import supervisor_snapshot, unit_snapshot
    from .control.tickets import read_stop_epoch
    from .control.processes import process_cgroup, RUNTIME_CGROUP
    layout = layout_for_root(root)
    if (os.geteuid() == 0 or not layout.managed
            or layout.descriptor.descriptor.service_uid != os.geteuid()
            or process_cgroup(os.getpid()) != RUNTIME_CGROUP):
        raise ControlError('control_parent_binding_forbidden')
    authority = state.validate_operation_with_credential(root, operation_id=operation_id,
        generation=generation, expected_phase=state.LifecyclePhase.RESERVED)
    operation = authority.active_operation
    if (operation is None or operation.operation_type.value != operation_type
            or operation_type not in {'update', 'restore'}):
        raise ControlError('control_parent_binding_changed')
    epoch = read_stop_epoch()
    selected = read_active_execution(root)
    unit = unit_snapshot()
    supervisor = supervisor_snapshot(layout.descriptor.descriptor, unit)
    backend = supervisor.get('children', {}).get('backend')
    if (not supervisor.get('launches_open') or not isinstance(backend, dict)
            or backend.get('pid') != os.getpid() or backend.get('start_identity') != state._process_start_identity(os.getpid())
            or supervisor.get('execution_sha256') != contract.execution_tuple_sha256(selected.execution)
            or os.environ.get('INVOCATION_ID') != supervisor.get('invocation')
            or selected.installation_id != layout.descriptor.descriptor.installation_id):
        raise ControlError('control_parent_binding_changed')
    value = parse_parent_execution({'schema_version': 1, 'installation_id': selected.installation_id,
        'operation_id': operation_id, 'operation_type': operation_type, 'original_generation': generation,
        'execution': contract.execution_tuple_payload(selected.execution),
        'supervisor_invocation_id': supervisor['invocation'], 'supervisor_pid': supervisor['pid'],
        'supervisor_start_identity': supervisor['start_identity'],
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'stop_epoch': epoch})
    if (state.inspect_state(root) != authority or read_active_execution(root) != selected
            or read_stop_epoch() != epoch or unit_snapshot() != unit):
        raise ControlError('control_parent_binding_changed')
    return value
