"""Unprivileged disposable preparation; never a lifecycle claim or launch grant."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import time

from .. import management_journal as contract, state
from ..configuration_store import ConfigurationStore
from ..execution_state import read_active_execution
from ..installation import InstallationStatus
from ..layout import layout_for_root
from ..runtime_build import prepare_runtime, verify_runtime
from .processes import process_cgroup
from .provision_worker import _private_record
from .requests import _process
from .rpc import ControlError, request
from .tickets import read_protected_json, read_stop_epoch
from .support import PREPARATION_STAGES


ROOT = Path('/opt/streamhome')
GRANTS = Path('/etc/streamhome/control/preparations')


def parse_grant(value: dict) -> dict:
    fields = {'schema_version', 'installation_id', 'preparation_id', 'expected_generation', 'previous',
              'expected_desired', 'supervisor', 'backend', 'supervisor_invocation_id', 'boot_id', 'stop_epoch',
              'control_manifest_sha256', 'expires_at_monotonic'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1):
        raise ControlError('control_preparation_invalid')
    for name in ('installation_id', 'preparation_id', 'boot_id'):
        contract._uuid(value[name], name)
    contract._integer(value['expected_generation'], 0, 'preparation generation')
    contract.parse_execution_tuple(value['previous'])
    for name in ('stop_epoch', 'control_manifest_sha256'):
        contract._text(value[name], contract._SHA256, name)
    invocation = contract._text(value['supervisor_invocation_id'], contract._INVOCATION, 'preparation supervisor')
    if not int(invocation, 16) or any(_process(value[name]) is None for name in ('supervisor', 'backend')):
        raise ControlError('control_preparation_invalid')
    reference = value['expected_desired']
    if not isinstance(reference, dict) or set(reference) != {'revision', 'sha256'}:
        raise ControlError('control_preparation_invalid')
    contract._uuid(reference['revision'], 'preparation desired revision')
    contract._text(reference['sha256'], contract._SHA256, 'preparation desired digest')
    if (type(value['expires_at_monotonic']) not in {int, float}
            or not 0 < value['expires_at_monotonic'] < float('inf')):
        raise ControlError('control_preparation_invalid')
    return value


def read_grant(preparation_id: str) -> dict:
    contract._uuid(preparation_id, 'preparation')
    value = parse_grant(read_protected_json(GRANTS / preparation_id / 'request.json'))
    if value['preparation_id'] != preparation_id:
        raise ControlError('control_preparation_invalid')
    return value


def grant_sha256(value: dict) -> str:
    parse_grant(value)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def require_current(value: dict) -> None:
    parse_grant(value)
    layout = layout_for_root(ROOT)
    selected = layout.descriptor.descriptor
    if (os.geteuid() == 0 or selected.service_uid != os.geteuid() or selected.status is not InstallationStatus.READY
            or selected.installation_id != value['installation_id'] or read_grant(value['preparation_id']) != value
            or value['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or value['stop_epoch'] != read_stop_epoch() or time.monotonic() >= value['expires_at_monotonic']):
        raise ControlError('control_preparation_changed')
    authority = state.inspect_state(ROOT)
    previous = contract.parse_execution_tuple(value['previous'])
    if (authority.schema_version != 2 or authority.active_operation is not None
            or authority.generation != value['expected_generation'] or read_active_execution(ROOT).execution != previous):
        raise ControlError('control_preparation_changed')
    descriptor = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with ConfigurationStore(descriptor, selected.installation_id) as store:
            manifest = store.inspect()
            if (manifest.desired.payload() != value['expected_desired']
                    or manifest.active.revision != previous.deployment_revision
                    or manifest.policy_revisions != previous.policy_revisions or manifest.provider_revisions != previous.provider_revisions):
                raise ControlError('control_preparation_changed')
    finally:
        os.close(descriptor)
    for name in ('supervisor', 'backend'):
        peer = _process(value[name])
        if state._process_start_identity(peer.pid) != peer.start_identity or process_cgroup(peer.pid) != '/system.slice/streamhome.service':
            raise ControlError('control_incarnation_changed')


def result(preparation_id: str) -> dict:
    value = read_grant(preparation_id)
    require_current(value)
    layout = layout_for_root(ROOT)
    payload = _private_record(layout.cache / 'preparation' / preparation_id / 'result.json')
    if (set(payload) != {'schema_version', 'grant_sha256', 'execution'} or type(payload['schema_version']) is not int
            or payload['schema_version'] != 1 or payload['grant_sha256'] != grant_sha256(value)):
        raise ControlError('control_preparation_result_invalid')
    candidate = contract.parse_execution_tuple(payload['execution'])
    previous = contract.parse_execution_tuple(value['previous'])
    if any(getattr(candidate, name) == getattr(previous, name)
           for name in ('runtime_bundle_id', 'python_environment_id', 'node_environment_id', 'web_bundle_id')):
        raise ControlError('control_preparation_result_invalid')
    # The journal's action matrix enforces unchanged commit, deployment,
    # assets, control and policy/provider identities before there is a claim.
    contract.parse_management_journal(contract.management_journal_payload(contract.ManagementJournal(
        value['installation_id'], preparation_id, value['expected_generation'] + 1,
        contract.ManagementAction.REPAIR_ENVIRONMENT, contract.ActivationMode.CUTOVER, preparation_id,
        previous, candidate, 'running', '1' * 32, value['supervisor_invocation_id'],
        contract.ManagementStage.RESERVED, None)))
    verify_runtime(ROOT, candidate)
    require_current(value)
    return payload


def main(preparation_id: str, *, control_manifest_sha256: str) -> int:
    value = read_grant(preparation_id)
    if (process_cgroup(os.getpid()) != f'/system.slice/streamhome-prepare@{preparation_id}.service'
            or value['control_manifest_sha256'] != control_manifest_sha256):
        raise ControlError('control_preparation_forbidden')
    require_current(value)
    layout = layout_for_root(ROOT)
    directory = layout.cache / 'preparation' / preparation_id
    state._ensure_private_directory(directory.parent, 'runtime preparation cache')
    directory.mkdir(mode=0o700)
    cancelled = False
    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
    for selected in (signal.SIGTERM, signal.SIGINT):
        signal.signal(selected, stop)
    def backend(action):
        return request(layout.runtime / 'backend.sock', expected_uid=os.geteuid(), expected_pid=value['backend']['pid'],
            installation_id=value['installation_id'], invocation=value['supervisor_invocation_id'],
            action=action, arguments={'preparation_id': preparation_id, 'grant_sha256': grant_sha256(value)}, timeout=5)
    registered = False
    last_check = 0
    def checkpoint():
        nonlocal last_check
        if cancelled:
            raise ControlError('control_operator_stopped')
        if time.monotonic() - last_check < 1:
            return
        require_current(value)
        if registered and backend('cache_preparation_status').get('registered') is not True:
            raise ControlError('control_preparation_changed')
        last_check = time.monotonic()
    @contextmanager
    def stage(description):
        if description not in PREPARATION_STAGES:
            raise ControlError('control_preparation_invalid')
        checkpoint()
        state._atomic_write_json(directory / 'progress.json', {'stage': description, 'status': 'running'})
        yield
        checkpoint()
        state._atomic_write_json(directory / 'progress.json', {'stage': description, 'status': 'done'})
    try:
        if backend('register_cache_preparation').get('registered') is not True:
            raise ControlError('control_preparation_changed')
        registered = True
        previous = contract.parse_execution_tuple(value['previous'])
        built = prepare_runtime(ROOT, application_commit=previous.application_commit,
            build_id=previous.application_build_id, stage=stage, checkpoint=checkpoint)
        base = ROOT / 'runtime'
        candidate = replace(previous, runtime_bundle_id=built.bundle_id, runtime_manifest_sha256=built.manifest_sha256,
            python_environment_id=built.python_id, python_executable=str(base / 'python' / built.python_id / 'venv/bin/python'),
            node_environment_id=built.node_id, node_executable=str(base / 'node' / built.node_id / 'bin/node'),
            web_bundle_id=built.web_id, web_dependency_target=str(base / 'web' / built.web_id / 'node_modules'),
            web_dist_target=str(base / 'web' / built.web_id / 'dist'))
        if built.web_sha256 != previous.web_asset_manifest_sha256:
            raise ControlError('control_repair_assets_changed')
        verify_runtime(ROOT, candidate)
        checkpoint()
        state._atomic_write_json(directory / 'result.json', {'schema_version': 1, 'grant_sha256': grant_sha256(value),
            'execution': contract.execution_tuple_payload(candidate)})
    finally:
        if registered:
            # Release is permitted only after the actual unit has no remaining
            # builder descendants. The backend independently checks its cgroup.
            backend('release_cache_preparation')
    return 0
