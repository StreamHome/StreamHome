"""Immutable execution evidence inside the existing UPDATE journal.

No additional claim, launch or recovery algorithm lives here. Older unbound
journals remain distinguishable; incomplete managed evidence is never legacy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
import re

from .management_journal import ExecutionTuple, execution_tuple_sha256, parse_execution_tuple
from .parent_execution import ParentExecution, parse_parent_execution
from .control.rpc import ControlError


FIELDS = frozenset({'managed_update_version', 'managed_execution', 'managed_candidate',
                    'managed_accepted_execution_sha256', 'managed_verification_execution_sha256',
                    'managed_database_checkpoint'})
PRESERVED = ('deployment_revision', 'control_bundle_id', 'control_manifest_sha256',
             'control_protocol_version', 'policy_revisions', 'provider_revisions')


@dataclass(frozen=True)
class DatabaseCheckpoint:
    operation_id: str
    generation: int
    previous_execution_sha256: str
    sha256: str
    size_bytes: int

    def payload(self) -> dict:
        return {'schema_version': 1, **self.__dict__}


def parse_checkpoint(value, parent: ParentExecution, generation: int) -> DatabaseCheckpoint | None:
    if value is None:
        return None
    fields = {'schema_version', 'operation_id', 'generation', 'previous_execution_sha256', 'sha256', 'size_bytes'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['operation_id'] != parent.operation_id or type(value['generation']) is not int
            or value['generation'] != parent.original_generation+1 or generation < value['generation']
            or value['previous_execution_sha256'] != execution_tuple_sha256(parent.execution)
            or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
            or type(value['size_bytes']) is not int or not 100 <= value['size_bytes'] < 2**63):
        raise ValueError('managed database checkpoint does not identify its original updater')
    return DatabaseCheckpoint(**{name: value[name] for name in fields if name != 'schema_version'})


@dataclass(frozen=True)
class UpdateExecution:
    parent: ParentExecution
    candidate: ExecutionTuple | None
    accepted: ExecutionTuple | None
    verification: ExecutionTuple | None
    checkpoint: DatabaseCheckpoint | None


def parse(payload: Mapping) -> UpdateExecution | None:
    present = FIELDS.intersection(payload)
    if not present:
        return None
    try:
        if (present != FIELDS or type(payload['managed_update_version']) is not int
                or payload['managed_update_version'] != 1):
            raise ValueError('incomplete managed update binding')
        parent = parse_parent_execution(payload['managed_execution'])
        generation = payload.get('lifecycle_generation')
        if (parent.operation_type != 'update' or parent.operation_id != payload.get('transaction_id')
                or type(generation) is not int or generation < parent.original_generation
                or parent.execution.application_commit != payload.get('previous_commit')):
            raise ValueError('managed update parent identity changed')
        if not isinstance(payload.get('target_commit'), str) or not re.fullmatch(r'[0-9a-f]{40}', payload['target_commit']):
            raise ValueError('managed update target identity is invalid')
        if (type(payload.get('automatic')) is not bool
                or payload.get('install_mode') not in {'automatic', 'when_idle', 'now'}
                or not isinstance(payload.get('update_branch'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', payload['update_branch'])
                or '..' in payload['update_branch']):
            raise ValueError('managed update launch request is invalid')
        candidate = None
        if payload['managed_candidate'] is not None:
            candidate = parse_execution_tuple(payload['managed_candidate'])
            if (candidate.application_commit != payload.get('target_commit')
                    or any(getattr(candidate, field) != getattr(parent.execution, field) for field in PRESERVED)
                    or any(getattr(candidate, field) == getattr(parent.execution, field) for field in (
                        'runtime_bundle_id', 'python_environment_id', 'node_environment_id', 'web_bundle_id'))):
                raise ValueError('managed update candidate changes unrelated identities or reuses generations')
        elif (payload.get('phase') not in {'queued', 'preflight', 'waiting_for_idle', 'failed', 'update_available'}
                or payload.get('lifecycle_phase') in {'exclusive', 'verifying'}):
            raise ValueError('managed update cutover lacks a prepared candidate')
        verification = None
        verification_identity = payload['managed_verification_execution_sha256']
        if verification_identity is not None:
            if candidate is None:
                raise ValueError('managed verification lacks a prepared candidate')
            choices = [selected for selected in (parent.execution, candidate)
                       if execution_tuple_sha256(selected) == verification_identity]
            if len(choices) != 1:
                raise ValueError('managed verification does not select an exact runtime')
            verification = choices[0]
        committed = payload.get('runtime_committed')
        if type(committed) is not bool:
            raise ValueError('managed update acceptance must be explicit')
        accepted = None
        identity = payload['managed_accepted_execution_sha256']
        if committed:
            if candidate is None:
                raise ValueError('managed update acceptance lacks a candidate')
            choices = [selected for selected in (parent.execution, candidate)
                       if execution_tuple_sha256(selected) == identity]
            if len(choices) != 1 or choices[0].application_commit != payload.get('current_commit'):
                raise ValueError('managed update acceptance does not select its exact runtime')
            accepted = choices[0]
            if verification != accepted:
                raise ValueError('managed acceptance differs from its verification runtime')
        elif identity is not None:
            raise ValueError('managed execution acceptance precedes the canonical update boundary')
        checkpoint = parse_checkpoint(payload['managed_database_checkpoint'], parent, generation)
        return UpdateExecution(parent, candidate, accepted, verification, checkpoint)
    except (KeyError, TypeError, ValueError, ControlError) as exc:
        raise ValueError('The managed UPDATE execution binding is invalid.') from exc


def require_transition(before: Mapping, after: Mapping) -> None:
    previous, current = parse(before), parse(after)
    if previous is None:
        if current is not None:
            raise ValueError('Managed UPDATE binding must be recorded at reservation.')
        return
    if current is None or current.parent != previous.parent:
        raise ValueError('The original UPDATE execution binding is immutable.')
    if any(before.get(field) != after.get(field) for field in ('target_commit', 'previous_commit', 'automatic', 'install_mode', 'update_branch')):
        raise ValueError('The original UPDATE request cannot be retargeted.')
    if previous.candidate is not None and current.candidate != previous.candidate:
        raise ValueError('The prepared UPDATE execution is immutable.')
    if previous.candidate is None and current.candidate is not None:
        if (before.get('lifecycle_phase') != 'reserved' or after.get('lifecycle_phase') != 'reserved'
                or before.get('lifecycle_generation') != after.get('lifecycle_generation')
                or before.get('phase') not in {'queued', 'preflight', 'waiting_for_idle'}):
            raise ValueError('An UPDATE candidate must be bound before cutover under the same owner.')
    if previous.accepted is not None and current.accepted != previous.accepted:
        raise ValueError('Accepted UPDATE execution cannot be replaced or revoked.')

    if previous.checkpoint is not None and current.checkpoint != previous.checkpoint:
        raise ValueError('The original UPDATE database checkpoint is immutable.')
    if previous.checkpoint is None and current.checkpoint is not None:
        if (before.get('lifecycle_phase') != 'exclusive' or after.get('lifecycle_phase') != 'exclusive'
                or before.get('phase') != 'stopping' or after.get('phase') != 'stopping'
                or before.get('lifecycle_generation') != current.parent.original_generation+1
                or before.get('lifecycle_generation') != after.get('lifecycle_generation')
                or current.accepted is not None or current.verification is not None):
            raise ValueError('The database checkpoint must be bound before target activation.')
    if previous.verification != current.verification:
        if (current.verification is None or after.get('lifecycle_phase') != 'verifying'
                or before.get('lifecycle_generation') != after.get('lifecycle_generation')
                or after.get('phase') not in {'starting', 'recovering', 'failed', 'rolled_back'}):
            raise ValueError('UPDATE verification selection requires its current guarded owner.')
        if (previous.verification is not None
                and not (previous.verification == previous.candidate
                         and current.verification == current.parent.execution
                         and previous.accepted is None
                         and after.get('phase') in {'failed', 'rolled_back'})):
            raise ValueError('UPDATE verification cannot switch away from its selected runtime.')
