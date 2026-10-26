"""RAM enrollment of one replacement supervisor for the original RESTORE.

The journal's original execution/supervisor history is never rewritten. A bound
OS receipt is subordinate to current canonical ownership and live scoped consent;
it cannot enroll a different process or authorize another service start.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .. import state
from ..execution_state import read_active_execution
from ..layout import layout_for_root
from ..management_journal import execution_tuple_sha256
from ..parent_execution import parse_parent_execution
from ..restore_controller import inspect_restore_journal
from .administration_lock import process_identity
from . import parent_grants
from .processes import RUNTIME_CGROUP, controller_peer, process_cgroup
from .privileged import unit_snapshot
from .resumption_contract import digest
from .resumption_ticket import bound_ticket
from .rpc import ControlError


def journal_identity(journal: dict) -> str:
    return digest({key:journal[key] for key in ('operation_id','managed_execution',
        'candidate_filename','candidate_size','candidate_sha256','created_at')})


@dataclass(frozen=True)
class ParentRuntimeBinding:
    installation_id: str
    operation_id: str
    generation: int
    execution_sha256: str
    journal_sha256: str
    parent_binding_sha256: str
    control_manifest_sha256: str
    plan_sha256: str
    receipt_sha256: str
    pid: int
    start_identity: str
    invocation: str

    def _completed(self, root: Path, authority, journal) -> bool:
        if journal_identity(journal)!=self.journal_sha256:
            raise ControlError('control_resumption_review_changed')
        operation=authority.active_operation
        if (operation is not None and operation.operation_id==self.operation_id
                or authority.generation<self.generation or journal['generation']!=self.generation
                or not journal['accepted'] or journal['progress'] not in {'succeeded','rolled_back'}
                or journal['outcome'] not in {'succeeded','rolled_back'}):
            return False
        active=read_active_execution(root)
        if (active.installation_id!=self.installation_id or active.generation>authority.generation
                or active.generation<=self.generation
                    and execution_tuple_sha256(active.execution)!=self.execution_sha256
                or active.generation>self.generation and active.operation_id==self.operation_id):
            raise ControlError('control_resumption_review_changed')
        return (state.inspect_state(root)==authority
                and inspect_restore_journal(root,self.operation_id)==journal)

    def require_current(self, root: Path) -> bool:
        if (os.getpid()!=self.pid or process_identity(self.pid)[0]!=self.start_identity
                or process_cgroup(self.pid)!=RUNTIME_CGROUP):
            raise ControlError('control_incarnation_changed')
        authority=state.inspect_state(root)
        journal=inspect_restore_journal(root,self.operation_id)
        if self._completed(root,authority,journal):return False
        try:
            current=require_binding(root,journal,self.invocation,
                control_manifest_sha256=self.control_manifest_sha256,own_supervisor=True)
            if current!=self:raise ControlError('control_resumption_review_changed')
            return True
        except (ControlError,OSError,ValueError):
            # Exact completion can race lease release or issuer exit. A fresh
            # accepted terminal observation may settle this RAM enrollment only.
            authority=state.inspect_state(root)
            journal=inspect_restore_journal(root,self.operation_id)
            if self._completed(root,authority,journal):return False
            raise


def require_binding(root: Path, journal: dict, invocation: str, *, control_manifest_sha256: str,
                    own_supervisor: bool=False) -> ParentRuntimeBinding:
    layout=layout_for_root(root)
    if not layout.managed or os.geteuid()==0 or layout.descriptor.descriptor.service_uid!=os.geteuid():
        raise ControlError('control_worker_forbidden')
    if journal['schema_version']!=2:raise ControlError('control_parent_binding_changed')
    bound=parse_parent_execution(journal['managed_execution'])
    authority=state.inspect_state(root);operation=authority.active_operation
    if (operation is None or operation.operation_type is not state.OperationType.RESTORE
            or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
            or operation.operation_id!=bound.operation_id or journal['operation_id']!=bound.operation_id
            or authority.generation!=journal['generation']
            or operation.phase not in {state.LifecyclePhase.VERIFYING,state.LifecyclePhase.RECOVERY_REQUIRED}
            or bound.installation_id!=layout.descriptor.descriptor.installation_id
            or bound.execution.control_manifest_sha256!=control_manifest_sha256
            or read_active_execution(root).execution!=bound.execution
            or invocation==bound.supervisor_invocation_id):
        raise ControlError('control_resumption_review_changed')
    grant=parent_grants.read_grant(bound.operation_id,authority.generation)
    parent_grants.require_binding(grant,bound)
    if grant['schema_version']!=2:raise ControlError('control_resumption_mode_unavailable')
    ticket=bound_ticket(grant['resumption_plan_id'],'candidate_start',invocation,
        installation_id=bound.installation_id,control_manifest_sha256=control_manifest_sha256)
    plan,receipt=ticket['plan'],ticket['receipt']
    if (plan['schema_version']!=2 or plan['operation_id']!=bound.operation_id
            or plan['generation']!=authority.generation or plan['review_id']!=grant['review_id']
            or plan['owner_type']!='restore' or plan['action']!='restore_recovery'
            or plan['parent_binding_sha256']!=parent_grants.binding_sha256(bound)
            or plan['recovery_action']!=grant['action']
            or plan['stages'][0]['execution_sha256']!=execution_tuple_sha256(bound.execution)):
        raise ControlError('control_resumption_review_changed')
    lease=state.read_controller_lease(root)
    if (lease is None or not state.controller_lease_is_current(lease)
            or lease.operation_id!=bound.operation_id or lease.generation!=authority.generation
            or lease.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER):
        raise ControlError('control_controller_changed')
    controller_peer(lease.pid,bound.operation_id)
    unit=unit_snapshot();pid=int(unit['MainPID'])
    if (unit['ActiveState'] not in {'active','activating'} or unit['InvocationID']!=invocation or pid<=1
            or process_cgroup(pid)!=RUNTIME_CGROUP or Path(f'/proc/{pid}').stat().st_uid!=os.geteuid()
            or own_supervisor and pid!=os.getpid()):
        raise ControlError('control_incarnation_changed')
    start,status=process_identity(pid)
    if status in {'Z','X'} or state.inspect_state(root)!=authority:
        raise ControlError('control_incarnation_changed')
    return ParentRuntimeBinding(bound.installation_id,bound.operation_id,authority.generation,
        execution_tuple_sha256(bound.execution),journal_identity(journal),parent_grants.binding_sha256(bound),
        control_manifest_sha256,receipt['plan_sha256'],digest(receipt),pid,start,invocation)


def initial_binding(root: Path, invocation: str, *, control_manifest_sha256: str):
    authority=state.inspect_state(root);operation=authority.active_operation
    if operation is None or operation.operation_type is not state.OperationType.RESTORE:return None
    journal=inspect_restore_journal(root,operation.operation_id)
    binding=require_binding(root,journal,invocation,control_manifest_sha256=control_manifest_sha256,
        own_supervisor=True)
    return journal,parse_parent_execution(journal['managed_execution']),binding
