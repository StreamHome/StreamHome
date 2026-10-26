"""Foreground, unprivileged owner of the managed backend/web unit.

Internal maintenance stops only this unit's exact cgroup children. Controllers
remain in separate operation units. An operator stop closes launch admission
before any asynchronous drain or child termination.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

from .. import management_journal as contract, management_store, state
from ..configuration_store import ConfigurationStore
from ..execution_state import read_active_execution
from ..installation import InstallationStatus
from ..layout import layout_for_root
from ..runtime_build import clean_environment, verify_runtime
from ..runtime_links import verify_links
from .processes import controller_peer, require_runtime_supervisor, runtime_children, stop_runtime_children
from .rpc import ControlError, Peer, Server, peer_credentials, request
from .notification import extending_startup, STARTUP_RECOVERY_SECONDS
from .tickets import require_first_start, read_stop_epoch
from .requests import read_management_request


ROOT = Path("/opt/streamhome")


def web_command(root: Path, execution) -> list[str]:
    # tsx's CLI spawns another Node process. Load its published Node hook
    # directly so the recorded leader is the process owning the HTTP listener.
    loader = root / "runtime/web" / execution.web_bundle_id / "node_modules/tsx/dist/loader.mjs"
    return [execution.node_executable, "--import", str(loader), str(root / "web/server.ts")]


@contextmanager
def immediate_stop_signals(close_admission):
    """Close the launch gate even while synchronous verification blocks the loop."""
    previous = {}
    def stop(signum, frame):
        close_admission()
    try:
        for value in (signal.SIGTERM, signal.SIGINT):
            previous[value] = signal.signal(value, stop)
        yield
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


def notify(value: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        raise ControlError("control_notify_unavailable")
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.connect(address)
        connection.sendall(value.encode())


def http_json(path: str = "/api/health/live") -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8000" + path, timeout=2) as response:
        payload = response.read(32769)
        if len(payload) > 32768:
            raise ControlError("control_health_invalid")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ControlError("control_health_invalid")
        return value


@dataclass
class Child:
    process: subprocess.Popen
    start: str
    token: str
    socket_identity: tuple[int, int] | None = None


class Supervisor:
    def __init__(self, *, control_bundle_id: str, control_manifest_sha256: str) -> None:
        require_runtime_supervisor()
        self.layout = layout_for_root(ROOT)
        if not self.layout.managed or self.layout.descriptor.descriptor.service_uid != os.geteuid():
            raise ControlError("control_supervisor_forbidden")
        self.installation = self.layout.descriptor.descriptor.installation_id
        self.control_bundle_id = contract._uuid(control_bundle_id, "supervisor control bundle")
        self.control_manifest_sha256 = contract._text(control_manifest_sha256, contract._SHA256, "supervisor control manifest")
        self.invocation = os.environ.get("INVOCATION_ID", "")
        state._ensure_private_directory(self.layout.runtime / "tmp", "managed temporary runtime directory")
        self.children: dict[str, Child] = {}
        self.launches_open = True
        self.lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.maintenance: list[asyncio.Server] = []
        self.server = Server(self.layout.runtime / "supervisor.sock", self.installation, self.invocation,
                             self.handle, allowed_uids=frozenset({0, os.geteuid()}))
        self.execution = None
        self.config = None
        self.last_error = None
        self.sequence = 0
        self.resumption_binding = None
        from .parent_handoff import Server as ParentHandoffServer
        self.parent_handoff = ParentHandoffServer(self.layout.runtime / 'parent-handoff.sock',
            authorize_offer=self.authorize_parent_offer, authorize_collect=self.authorize_parent_collect,
            start_operation=self.start_parent_operation)

    def _parent_journal(self, operation_id: str, generation: int, *, phase=None):
        from ..parent_execution import parse_parent_execution
        from ..restore_controller import inspect_restore_journal
        if not self.launches_open:
            raise ControlError('control_operator_stopped')
        authority = state.inspect_state(ROOT)
        operation = authority.active_operation
        if (operation is None or operation.operation_id != operation_id or authority.generation != generation
                or operation.operation_type is not state.OperationType.RESTORE
                or operation.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
                or phase is not None and operation.phase is not phase):
            raise ControlError('control_authority_changed')
        journal = inspect_restore_journal(ROOT, operation_id)
        if journal['schema_version'] != 2 or journal['generation'] != generation:
            raise ControlError('control_parent_binding_changed')
        bound = parse_parent_execution(journal['managed_execution'])
        from .parent_resumption_runtime import ParentRuntimeBinding, journal_identity
        enrollment = getattr(self, 'resumption_binding', None)
        if isinstance(enrollment, ParentRuntimeBinding):
            if (not enrollment.require_current(ROOT) or enrollment.invocation != self.invocation
                    or enrollment.operation_id != operation_id or enrollment.generation != generation
                    or enrollment.journal_sha256 != journal_identity(journal)
                    or bound.installation_id != self.installation or bound.execution != self.execution
                    or read_active_execution(ROOT).execution != bound.execution):
                raise ControlError('control_parent_binding_changed')
            return journal, bound
        if (bound.installation_id != self.installation or bound.supervisor_invocation_id != self.invocation
                or bound.supervisor_pid != os.getpid() or bound.supervisor_start_identity != state._process_start_identity(os.getpid())
                or bound.stop_epoch != read_stop_epoch()
                or bound.boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                or bound.execution != self.execution or read_active_execution(ROOT).execution != bound.execution):
            raise ControlError('control_parent_binding_changed')
        return journal, bound

    async def authorize_parent_offer(self, binding, peer):
        from .administration_lock import process_identity
        async with self.lock:
            if (peer.uid != os.geteuid() or binding.installation_id != self.installation
                    or binding.supervisor_invocation_id != self.invocation or binding.operation_type != 'restore'):
                raise ControlError('control_peer_forbidden')
            journal, bound = self._parent_journal(binding.operation_id, binding.generation,
                                                 phase=state.LifecyclePhase.RESERVED)
            backend = self.children.get('backend')
            if (backend is None or backend.process.pid != peer.pid or bound.original_generation != binding.generation
                    or journal['accepted'] or journal['progress'] not in {'claimed','drained'}):
                raise ControlError('control_peer_forbidden')
            start, status = process_identity(peer.pid)
            if start != backend.start or status in {'Z','X'}:
                raise ControlError('control_process_changed')
            if not self.launches_open:raise ControlError('control_operator_stopped')

    async def authorize_parent_collect(self, binding, peer):
        from .privileged import systemctl
        from .processes import process_cgroup
        async with self.lock:
            self._parent_journal(binding.operation_id, binding.generation, phase=state.LifecyclePhase.RESERVED)
            controller_peer(peer.pid, binding.operation_id)
            if peer.uid != os.geteuid():raise ControlError('control_peer_forbidden')
            name = 'streamhome-operation@' + binding.operation_id + '.service'
            raw = systemctl('show', name, '--property=ActiveState,MainPID,InvocationID,FragmentPath,ControlGroup,User')
            actor = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
            args = Path(f'/proc/{peer.pid}/cmdline').read_bytes().split(b'\0')
            if (actor.get('ActiveState') not in {'active','activating'} or actor.get('MainPID') != str(peer.pid)
                    or actor.get('User') != 'streamhome' or not actor.get('InvocationID')
                    or actor.get('FragmentPath') != '/etc/systemd/system/streamhome-operation@.service'
                    or actor.get('ControlGroup') != '/system.slice/' + name
                    or process_cgroup(peer.pid) != actor['ControlGroup']
                    or args != [b'/usr/bin/python3',b'-I',b'/usr/local/libexec/streamhome/provision',
                                b'--operation',binding.operation_id.encode(),b'']):
                raise ControlError('control_controller_forbidden')
            self._parent_journal(binding.operation_id,binding.generation,phase=state.LifecyclePhase.RESERVED)

    async def start_parent_operation(self, binding):
        async with self.lock:
            self._parent_journal(binding.operation_id, binding.generation, phase=state.LifecyclePhase.RESERVED)
        from .privileged import _OS_ENV
        result = await asyncio.to_thread(subprocess.run,
            ['/usr/bin/systemctl','--no-pager','start','streamhome-operation@'+binding.operation_id+'.service'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd='/', env=_OS_ENV, timeout=15)
        if result.returncode:raise ControlError('control_controller_start_failed')

    def _restore_controller(self, peer, arguments):
        if (set(arguments) != {'operation_id','generation'} or type(arguments['generation']) is not int
                or peer.uid != os.geteuid()):
            raise ControlError('control_message_invalid')
        controller_peer(peer.pid,arguments['operation_id'])
        journal,bound = self._parent_journal(arguments['operation_id'],arguments['generation'])
        authority = state.inspect_state(ROOT)
        lease = state.read_controller_lease(ROOT)
        if (lease is None or not state.controller_lease_is_current(lease) or lease.pid != peer.pid
                or lease.operation_id != arguments['operation_id'] or lease.generation != arguments['generation']
                or lease.owner_kind is not state.OwnerKind.RESTORE_CONTROLLER
                or authority.active_operation.phase not in {state.LifecyclePhase.EXCLUSIVE,
                    state.LifecyclePhase.VERIFYING,state.LifecyclePhase.RECOVERY_REQUIRED}):
            raise ControlError('control_controller_changed')
        from ..parent_recovery import require_controller_permission
        require_controller_permission(ROOT, arguments['operation_id'], arguments['generation'])
        return journal,bound

    def _update_journal(self, operation_id: str, generation: int, *, fence_only=False):
        from ..managed_update import bound_journal
        if not self.launches_open:
            raise ControlError('control_operator_stopped')
        if fence_only:
            from ..managed_update import read_fence_binding
            authority, journal, selected = read_fence_binding(ROOT, operation_id)
        else:
            authority, journal, selected = bound_journal(ROOT, operation_id)
        bound = selected.parent
        active = read_active_execution(ROOT)
        from .update_resumption_runtime import UpdateRuntimeBinding, identity
        enrollment = getattr(self, 'resumption_binding', None)
        if isinstance(enrollment, UpdateRuntimeBinding):
            if (not enrollment.require_current(ROOT) or enrollment.invocation != self.invocation
                    or enrollment.operation_id != operation_id or enrollment.generation != generation
                    or authority.generation != generation or enrollment.journal_sha256 != identity(journal)
                    or bound.installation_id != self.installation
                    or self.execution != selected.verification
                    or contract.execution_tuple_sha256(self.execution) != enrollment.execution_sha256):
                raise ControlError('control_parent_binding_changed')
            return authority, journal, selected
        if (authority.generation != generation or bound.installation_id != self.installation
                or bound.supervisor_invocation_id != self.invocation or bound.supervisor_pid != os.getpid()
                or bound.supervisor_start_identity != state._process_start_identity(os.getpid())
                or bound.stop_epoch != read_stop_epoch()
                or bound.boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                or bound.execution.control_bundle_id != self.control_bundle_id
                or bound.execution.control_manifest_sha256 != self.control_manifest_sha256
                or self.execution not in (bound.execution, selected.candidate)
                or active.installation_id != self.installation
                or active.execution not in (bound.execution, selected.candidate)):
            raise ControlError('control_parent_binding_changed')
        return authority, journal, selected

    def _update_controller(self, peer: Peer, arguments: dict):
        from ..managed_update import require_delegate_peer, require_owner_peer
        if (set(arguments) != {'operation_id', 'generation'} or type(arguments['generation']) is not int
                or peer.uid != os.geteuid()):
            raise ControlError('control_message_invalid')
        authority, journal, selected = self._update_journal(arguments['operation_id'], arguments['generation'])
        lease = state.read_controller_lease(ROOT)
        if (lease is None or lease.operation_id != arguments['operation_id'] or lease.generation != arguments['generation']
                or authority.active_operation.phase not in {state.LifecyclePhase.EXCLUSIVE,
                    state.LifecyclePhase.VERIFYING, state.LifecyclePhase.RECOVERY_REQUIRED}):
            raise ControlError('control_controller_changed')
        observed = (require_owner_peer(ROOT, lease, peer.pid) if peer.pid == lease.pid
            else require_delegate_peer(ROOT, lease, peer.pid))
        if observed != (authority, journal, selected):
            raise ControlError('control_parent_binding_changed')
        return authority, journal, selected

    def _update_preflight_owner(self, peer: Peer, arguments: dict):
        from ..managed_update import require_owner_peer
        if (set(arguments) != {'operation_id', 'generation'} or type(arguments['generation']) is not int
                or peer.uid != os.geteuid()):
            raise ControlError('control_message_invalid')
        authority, journal, bound = self._update_journal(arguments['operation_id'], arguments['generation'])
        lease = state.read_controller_lease(ROOT)
        if (authority.active_operation.phase is not state.LifecyclePhase.RESERVED
                or authority.generation != bound.parent.original_generation+1 or journal['phase'] != 'preflight'
                or bound.accepted is not None or bound.verification is not None
                or self.execution != bound.parent.execution or lease is None or lease.pid != peer.pid
                or require_owner_peer(ROOT, lease, peer.pid) != (authority, journal, bound)):
            raise ControlError('control_controller_changed')
        return authority, journal, bound

    def _update_reservation(self, peer: Peer, arguments: dict):
        from .reader_support import require_managed_update_reader
        from .administration_lock import process_identity
        from .processes import RUNTIME_CGROUP, process_cgroup
        if (set(arguments) != {'operation_id', 'generation'} or type(arguments['generation']) is not int
                or peer.uid != os.geteuid()):
            raise ControlError('control_message_invalid')
        authority, journal, bound = self._update_journal(arguments['operation_id'], arguments['generation'])
        require_managed_update_reader(bound.parent.execution)
        backend = self.children.get('backend')
        lease = state.read_controller_lease(ROOT)
        if (authority.active_operation.phase is not state.LifecyclePhase.RESERVED
                or authority.generation != bound.parent.original_generation or journal['phase'] != 'queued'
                or bound.candidate is not None or bound.accepted is not None or bound.verification is not None
                or backend is None or peer.pid != backend.process.pid
                or process_cgroup(peer.pid) != RUNTIME_CGROUP
                or lease is not None and lease.generation >= authority.generation):
            raise ControlError('control_update_reservation_changed')
        start, status = process_identity(peer.pid)
        if start != backend.start or status in {'Z', 'X'}:
            raise ControlError('control_process_changed')
        return authority, journal, bound

    def _initial_update_owner(self, original):
        from ..managed_update import require_owner_peer
        authority, journal, bound = self._update_journal(original[2].parent.operation_id, original[0].generation+1)
        lease = state.read_controller_lease(ROOT)
        if (bound.parent != original[2].parent or authority.active_operation.phase is not state.LifecyclePhase.RESERVED
                or lease is None or lease.generation != authority.generation
                or require_owner_peer(ROOT, lease, lease.pid) != (authority, journal, bound)):
            raise ControlError('control_controller_changed')
        return {'operation_id': bound.parent.operation_id, 'generation': authority.generation,
            'controller_pid': lease.pid}

    async def start_update_operation(self, peer: Peer, arguments: dict):
        from .administration import operation_unit
        from .privileged import _OS_ENV
        original = self._update_reservation(peer, arguments)
        actor = operation_unit(arguments['operation_id'])
        if actor['ActiveState'] not in {'inactive', 'failed'} or actor['MainPID'] != '0':
            raise ControlError('control_controller_busy')
        if self._update_reservation(peer, arguments) != original:
            raise ControlError('control_update_reservation_changed')
        result = await asyncio.to_thread(subprocess.run,
            ['/usr/bin/systemctl', '--no-pager', 'start',
                'streamhome-operation@'+arguments['operation_id']+'.service'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd='/', env=_OS_ENV, timeout=15)
        if result.returncode:
            raise ControlError('control_controller_start_failed')
        deadline = time.monotonic()+10
        while self.launches_open:
            current = state.inspect_state(ROOT)
            if current.generation != original[0].generation:
                operation = current.active_operation
                if (current.generation != original[0].generation+1 or operation is None
                        or operation.operation_id != arguments['operation_id']
                        or operation.operation_type is not state.OperationType.UPDATE
                        or operation.owner_kind is not state.OwnerKind.UPDATER
                        or operation.phase is not state.LifecyclePhase.RESERVED):
                    raise ControlError('control_controller_changed')
                from ..update_controller import UpdateControllerError
                try:
                    return self._initial_update_owner(original)
                except (ControlError, UpdateControllerError, state.LifecycleError):
                    # The original ownership CAS can precede journal/lease
                    # publication. Wait only for this exact transferred owner;
                    # no acknowledgement or second OS start follows ambiguity.
                    pass
            elif self._update_reservation(peer, arguments) != original:
                raise ControlError('control_update_reservation_changed')
            if time.monotonic() >= deadline:
                raise ControlError('control_controller_start_timeout')
            await asyncio.sleep(.05)
        raise ControlError('control_operator_stopped')

    def initial_resumption(self):
        from .update_resumption_runtime import initial_binding as update_binding
        update = update_binding(ROOT, self.invocation, control_manifest_sha256=self.control_manifest_sha256)
        if update is not None:
            return update[0], update[2]
        from .parent_resumption_runtime import initial_binding as parent_binding
        parent = parent_binding(ROOT, self.invocation,
            control_manifest_sha256=self.control_manifest_sha256)
        if parent is not None:
            journal, bound, binding = parent
            return journal, binding
        from .resumption_runtime import initial_binding
        return initial_binding(ROOT, self.invocation,
                               control_manifest_sha256=self.control_manifest_sha256)

    def require_resumption(self) -> None:
        if self.resumption_binding is not None and not self.resumption_binding.require_current(ROOT):
            self.resumption_binding = None

    def _deployment(self, execution):
        descriptor = os.open(self.layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            with ConfigurationStore(descriptor, self.installation) as store:
                manifest = store.inspect()
                references = [reference for reference in (manifest.active, manifest.desired) if reference.revision == execution.deployment_revision]
                if not references or execution.deployment_revision in manifest.quarantined:
                    raise ControlError("control_deployment_changed")
                return store.read_revision(references[0]).settings
        finally:
            os.close(descriptor)

    def _controller(self, peer: Peer, arguments: dict, *, phase: state.LifecyclePhase):
        if set(arguments) != {"operation_id", "generation"} or type(arguments["generation"]) is not int:
            raise ControlError("control_message_invalid")
        controller_peer(peer.pid, arguments["operation_id"])
        lease = state.read_controller_lease(ROOT)
        authority = state.inspect_state(ROOT)
        if (lease is None or not state.controller_lease_is_current(lease) or lease.pid != peer.pid
                or lease.operation_id != arguments["operation_id"] or lease.generation != arguments["generation"]):
            raise ControlError("control_controller_changed")
        operation = authority.active_operation
        if (operation is None or operation.operation_type is not state.OperationType.MANAGEMENT
                or operation.phase is not phase or authority.generation != lease.generation
                or operation.operation_id != lease.operation_id):
            raise ControlError("control_authority_changed")
        journal = management_store.inspect_management_journal(ROOT, operation.operation_id)
        contract.validate_active_journal(journal, authority)
        if journal.installation_id != self.installation:
            raise ControlError("control_incarnation_changed")
        if journal.supervisor_invocation_id != self.invocation:
            from .resumption_runtime import require_binding
            require_binding(ROOT, journal, self.invocation,
                            control_manifest_sha256=self.control_manifest_sha256,
                            own_supervisor=True, allow_original=False)
        from .recovery_requests import effective_request
        reviewed = effective_request(journal, original=read_management_request(journal.operation_id))
        if (journal.review_id != reviewed.review_id or journal.generation != reviewed.expected_generation + 1
                or journal.previous != reviewed.previous or journal.candidate != reviewed.candidate):
            raise ControlError("control_request_changed")
        if (reviewed.stop_epoch != read_stop_epoch()
                or reviewed.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
            raise ControlError("control_operator_stopped")
        return journal

    async def handle(self, peer: Peer, action: str, arguments: dict) -> dict:
        async with self.lock:
            if action == "snapshot":
                if arguments:
                    raise ControlError("control_message_invalid")
                return self.snapshot()
            if action == "stop_notice":
                if peer.uid != 0 or arguments:
                    raise ControlError("control_peer_forbidden")
                self.launches_open = False
                self.stopping.set()
                return {"stopping": True}
            if not self.launches_open:
                raise ControlError("control_operator_stopped")
            self.require_resumption()
            if action == 'update_preflight_readiness':
                original = self._update_preflight_owner(peer, arguments)
                try:
                    ready = await asyncio.to_thread(self.selected_readiness) == 'general'
                except (OSError, ValueError, urllib.error.URLError):
                    ready = False
                if self._update_preflight_owner(peer, arguments) != original:
                    raise ControlError('control_controller_changed')
                return {'ready': ready}
            if action == 'start_update_operation':
                return await self.start_update_operation(peer, arguments)
            if action == 'update_transport_readiness':
                authority, journal, bound = self._update_controller(peer, arguments)
                if (authority.active_operation.phase is not state.LifecyclePhase.VERIFYING
                        or journal['phase'] not in {'recovering', 'succeeded', 'rolled_back'} or bound.accepted is None
                        or bound.accepted != self.execution or set(self.children) != {'backend', 'web'}):
                    raise ControlError('control_authority_changed')
                try:
                    ready = await asyncio.to_thread(self.update_transport_ready)
                except (OSError, ValueError, urllib.error.URLError):
                    ready = False
                self._update_controller(peer, arguments)
                return {'ready': ready}
            if action in {'update_start_candidate', 'update_start_previous'}:
                update = self._update_controller(peer, arguments)
                authority, journal, bound = update
                selected = bound.candidate if action == 'update_start_candidate' else bound.parent.execution
                if (authority.active_operation.phase is not state.LifecyclePhase.VERIFYING
                        or selected is None or bound.verification != selected
                        or journal['phase'] not in ({'starting', 'recovering'}
                            if action == 'update_start_candidate' else {'failed', 'rolled_back', 'recovering'})
                        or bound.accepted is not None and bound.accepted != selected):
                    raise ControlError('control_authority_changed')
                from .update_resumption_runtime import UpdateRuntimeBinding
                if (isinstance(getattr(self, 'resumption_binding', None), UpdateRuntimeBinding)
                        and set(self.children) == {'backend', 'web'} and self.execution == selected):
                    self._update_controller(peer, arguments)
                    await self.wait_update_guarded(peer, arguments)
                    return {'launched': True, 'build_id': selected.application_build_id}
                if self.children or runtime_children():
                    raise ControlError('control_children_busy')
                from ..update_checkpoint import require_launch_database
                require_launch_database(ROOT, bound)
                self._update_controller(peer, arguments)
                await self.close_maintenance()
                await self.launch(selected, update=update,
                    authorize=lambda: self._update_controller(peer, arguments))
                await self.wait_update_guarded(peer, arguments)
                return {'launched': True, 'build_id': selected.application_build_id}
            if action in {'update_stop_children', 'update_assert_stopped', 'update_maintenance'}:
                self._update_controller(peer, arguments)
                if action == 'update_stop_children':
                    await self.stop_children()
                    self._update_controller(peer, arguments)
                    await self.start_maintenance()
                    return {'stopped': True}
                if self.children or runtime_children():
                    raise ControlError('control_children_busy')
                self._update_controller(peer, arguments)
                if action == 'update_maintenance':
                    await self.start_maintenance()
                    return {'maintenance': True}
                return {'stopped': True}
            if action in {'restore_stop_children','restore_maintenance','restore_backend','restore_finish_web',
                          'restore_web_readiness'}:
                journal,bound = self._restore_controller(peer,arguments)
                if action == 'restore_stop_children':
                    await self.stop_children()
                    self.execution,self.config = bound.execution,self._deployment(bound.execution)
                    await self.start_maintenance()
                    return {'stopped':True}
                if action == 'restore_maintenance':
                    if self.children or runtime_children():raise ControlError('control_children_busy')
                    await self.start_maintenance();return {'maintenance':True}
                authority = state.inspect_state(ROOT)
                if authority.active_operation.phase is not state.LifecyclePhase.VERIFYING:
                    raise ControlError('control_authority_changed')
                if action == 'restore_backend':
                    if journal['progress'] not in {'verifying_candidate','verifying_rollback','accepted','normal_recovery'}:
                        raise ControlError('control_authority_changed')
                    from .parent_resumption_runtime import ParentRuntimeBinding
                    if (isinstance(self.resumption_binding, ParentRuntimeBinding)
                            and set(self.children) == {'backend'}):
                        backend = self.children['backend']
                        if (backend.process.poll() is not None
                                or state._process_start_identity(backend.process.pid) != backend.start):
                            raise ControlError('control_process_changed')
                        return {'backend_pid':backend.process.pid,'build_id':bound.execution.application_build_id}
                    if self.children or runtime_children():raise ControlError('control_children_busy')
                    await self.close_maintenance()
                    await self.launch(bound.execution,restore=(journal,bound),backend_only=True,
                                      authorize=lambda:self._restore_controller(peer,arguments))
                    await self.start_maintenance(backend=False)
                    return {'backend_pid':self.children['backend'].process.pid,'build_id':bound.execution.application_build_id}
                if action == 'restore_web_readiness':
                    if (not journal['accepted'] or journal['progress'] != 'normal_recovery'
                            or set(self.children) != {'backend','web'}):
                        raise ControlError('control_authority_changed')
                    try:
                        ready = await asyncio.to_thread(self.restore_transport_ready)
                    except ControlError as exc:
                        if exc.code not in {'control_unavailable','control_reply_incomplete','control_timeout'}:
                            raise
                        return {'ready':False}
                    except (OSError, ValueError, urllib.error.URLError):
                        return {'ready':False}
                    if not ready:return {'ready':False}
                    self._restore_controller(peer,arguments)
                    return {'ready':True,'build_id':bound.execution.application_build_id,
                            'web_pid':self.children['web'].process.pid}
                if (not journal['accepted'] or journal['progress'] != 'normal_recovery'
                        or set(self.children) != {'backend'}):
                    raise ControlError('control_authority_changed')
                await self.close_maintenance()
                self._restore_controller(peer,arguments)
                self._spawn('web',web_command(ROOT,bound.execution),self._environment(bound.execution,self.config),ROOT/'web')
                return {'web_pid':self.children['web'].process.pid,'build_id':bound.execution.application_build_id}
            if action in {"recover_children", "assert_recovery_stopped"}:
                from ..management_recovery_store import require_takeover_source
                journal = self._controller(peer, arguments, phase=state.LifecyclePhase.RECOVERY_REQUIRED)
                require_takeover_source(ROOT, journal)
                if action == 'recover_children':
                    await self.stop_children()
                    self.execution = contract.selected_execution(journal) if journal.acceptance else journal.previous
                    self.config = self._deployment(self.execution)
                    await self.start_maintenance()
                if self.children or runtime_children():
                    raise ControlError("control_children_busy")
                return {"stopped": True, "sequence": self.sequence}
            if action == "stop_children":
                journal = self._controller(peer, arguments, phase=state.LifecyclePhase.EXCLUSIVE)
                if journal.stage is not contract.ManagementStage.EXCLUSIVE or journal.acceptance is not None:
                    raise ControlError("control_authority_changed")
                await self.stop_children()
                self.execution = journal.previous
                self.config = self._deployment(journal.previous)
                await self.start_maintenance()
                return {"stopped": True, "sequence": self.sequence}
            if action in {"launch_candidate", "launch_recovered"}:
                journal = self._controller(peer, arguments, phase=state.LifecyclePhase.VERIFYING)
                accepted_recovery = False
                if action == 'launch_recovered':
                    from ..management_recovery_store import require_takeover_source
                    require_takeover_source(ROOT, journal)
                    accepted_recovery = journal.stage is contract.ManagementStage.ACCEPTED and journal.acceptance is not None
                if (not accepted_recovery and (journal.stage is not contract.ManagementStage.VERIFYING or journal.acceptance is not None)
                        or self.children):
                    raise ControlError("control_candidate_unavailable")
                await self.close_maintenance()
                await self.launch(contract.selected_execution(journal), journal=journal,
                    authorize=lambda: self._controller(peer, arguments, phase=state.LifecyclePhase.VERIFYING))
                return {"launched": True, "sequence": self.sequence, "backend_pid": self.children["backend"].process.pid}
            if action == "assert_stopped":
                self._controller(peer, arguments, phase=state.LifecyclePhase.EXCLUSIVE)
                if self.children or runtime_children():
                    raise ControlError("control_children_busy")
                return {"stopped": True, "sequence": self.sequence}
            raise ControlError("control_action_unsupported")

    def snapshot(self) -> dict:
        return {
            "installation_id": self.installation, "invocation": self.invocation, "pid": os.getpid(),
            "start_identity": state._process_start_identity(os.getpid()), "launches_open": self.launches_open,
            "sequence": self.sequence, "error_code": self.last_error,
            "execution_sha256": contract.execution_tuple_sha256(self.execution) if self.execution else None,
            "children": {name: {"pid": child.process.pid, "start_identity": child.start} for name, child in self.children.items()},
        }

    def _spawn(self, service: str, arguments: list[str], environment: dict[str, str], cwd: Path) -> None:
        token = secrets.token_hex(32)
        environment = {**environment, "STREAMHOME_SERVICE": service, "STREAMHOME_INSTANCE_TOKEN": token}
        # The signal handler may close admission after the preceding child was
        # launched. Each child must independently honor that decision.
        self.require_resumption()
        if not self.launches_open:
            raise ControlError("control_operator_stopped")
        process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, start_new_session=True)
        identity = state._process_start_identity(process.pid)
        self.children[service] = Child(process, identity, token)
        # Compatible private ownership evidence for existing listener checks.
        payload = {"version": 1, "root": str(ROOT), "service": service, "token": token,
                   "pid": process.pid, "ppid": os.getpid(), "pgid": process.pid, "sid": process.pid,
                   "start_ticks": identity, "recorded_at": time.time()}
        state._atomic_write_json(self.layout.runtime / f"{service}.runtime.json", payload)

    async def launch(self, execution, *, journal=None, restore=None, update=None, backend_only=False, authorize=None) -> None:
        if not self.launches_open or self.children or runtime_children():
            raise ControlError("control_children_busy")
        fresh_layout = layout_for_root(ROOT)
        if fresh_layout.descriptor.descriptor.installation_id != self.installation or fresh_layout.descriptor.descriptor.service_uid != os.geteuid():
            raise ControlError("control_incarnation_changed")
        self.layout = fresh_layout
        if execution.control_bundle_id != self.control_bundle_id or execution.control_manifest_sha256 != self.control_manifest_sha256 or execution.control_protocol_version != 1:
            raise ControlError("control_bundle_mismatch")
        verify_runtime(ROOT, execution)
        verify_links(ROOT, execution)
        if not self.launches_open:
            raise ControlError("control_operator_stopped")
        if self.layout.descriptor.descriptor.status is InstallationStatus.PROVISIONING:
            require_first_start(self.layout, execution, self.invocation)
        config = self._deployment(execution)
        if state.inspect_state(ROOT).active_operation is not None and journal is None and restore is None and update is None:
            raise ControlError("control_recovery_required")
        if sum(value is not None for value in (journal, restore, update)) > 1 or backend_only and restore is None:
            raise ControlError('control_message_invalid')
        if journal is None and restore is None and update is None:
            descriptor = os.open(self.layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                with ConfigurationStore(descriptor, self.installation) as store:
                    manifest = store.inspect()
                    if manifest.active != manifest.desired and manifest.desired.revision not in manifest.quarantined:
                        raise ControlError("control_pending_deployment")
            finally:
                os.close(descriptor)
        environment = self._environment(execution,config)
        if journal is not None:
            environment.update(STREAMHOME_LIFECYCLE_STARTUP_MODE="management_verify",
                               STREAMHOME_LIFECYCLE_OPERATION_ID=journal.operation_id,
                               STREAMHOME_LIFECYCLE_GENERATION=str(journal.generation))
        if restore is not None:
            environment.update(STREAMHOME_LIFECYCLE_STARTUP_MODE='restore_verify',
                STREAMHOME_LIFECYCLE_OPERATION_ID=restore[0]['operation_id'],
                STREAMHOME_LIFECYCLE_GENERATION=str(restore[0]['generation']))
        if update is not None:
            from ..managed_update import read_commit_token
            environment.update(STREAMHOME_LIFECYCLE_STARTUP_MODE='update_verify',
                STREAMHOME_LIFECYCLE_OPERATION_ID=update[2].parent.operation_id,
                STREAMHOME_LIFECYCLE_GENERATION=str(update[0].generation),
                STREAMHOME_UPDATE_TRANSACTION=update[2].parent.operation_id,
                STREAMHOME_UPDATE_COMMIT_TOKEN=read_commit_token(ROOT, update[2].parent.operation_id))
        if not self.launches_open:
            raise ControlError("control_operator_stopped")
        if update is not None and (authorize is None or authorize() != update):
            raise ControlError('control_controller_changed')
        if journal is not None and (authorize is None or authorize() != journal):
            raise ControlError("control_controller_changed")
        if restore is not None and (authorize is None or authorize() != restore):
            raise ControlError('control_controller_changed')
        if self.layout.descriptor.descriptor.status is InstallationStatus.PROVISIONING:
            require_first_start(self.layout, execution, self.invocation)
        self.execution, self.config = execution, config
        self.last_error = None
        self.sequence += 1
        self._spawn("backend", [execution.python_executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--no-proxy-headers"], environment, ROOT / "server")
        if not backend_only:self._spawn("web", web_command(ROOT, execution), environment, ROOT / "web")

    def _environment(self,execution,config):
        environment = clean_environment(self.layout.state,Path(execution.node_executable).parent)
        environment.update({
            'TMPDIR':str(self.layout.runtime/'tmp'),'STREAMHOME_INSTANCE_ROOT':str(ROOT),
            'STREAMHOME_BUILD_ID':execution.application_build_id,'STREAMHOME_INSTALLATION_ID':self.installation,
            'STREAMHOME_RUNTIME_TUPLE_SHA256':contract.execution_tuple_sha256(execution),
            'STREAMHOME_DEPLOYMENT_REVISION':execution.deployment_revision,
            'WEB_PORT':str(config.web_port),'WEB_BIND':config.web_bind,'PUBLIC_URL':config.public_origin,
            'TRUSTED_PROXY_CIDRS':','.join(config.trusted_proxy_cidrs),'ALLOWED_ORIGINS':','.join(config.allowed_origins),
            'NODE_ENV':'production','INVOCATION_ID':self.invocation,'TSX_DISABLE_CACHE':'1'})
        return environment

    async def stop_children(self) -> None:
        # Never reap a leader before its cgroup descendants have been stopped.
        # This includes external tools that created their own process sessions.
        self._remember_backend_socket()
        backend = self.children.get("backend")
        await stop_runtime_children()
        for child in self.children.values():
            child.process.wait(timeout=5)
        self.children.clear()
        self.sequence += 1
        # A dead backend cannot serve this endpoint. Refuse foreign files and
        # remove only the exact owned socket in the private runtime directory.
        endpoint = self.layout.runtime / "backend.sock"
        if endpoint.exists():
            import stat
            metadata = endpoint.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or backend is None or backend.socket_identity != (metadata.st_dev, metadata.st_ino)):
                raise ControlError("control_socket_untrusted")
            endpoint.unlink()
            state._fsync_directory(self.layout.runtime)

    def _remember_backend_socket(self) -> None:
        child = self.children.get("backend")
        if child is None or child.socket_identity is not None:
            return
        endpoint = self.layout.runtime / "backend.sock"
        try:
            before = endpoint.lstat()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.2)
                connection.connect(str(endpoint))
                peer = peer_credentials(connection)
                if peer.pid != child.process.pid or peer.uid != os.geteuid() or state._process_start_identity(peer.pid) != child.start:
                    raise ControlError("control_socket_untrusted")
            after = endpoint.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ControlError("control_socket_changed")
            child.socket_identity = (after.st_dev, after.st_ino)
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
            return

    async def _maintenance_connection(self, reader, writer) -> None:
        try:
            async with asyncio.timeout(3):
                line = await reader.readline()
                if len(line) > 4096:
                    return
                payload = json.dumps({"status": "maintenance", "code": self.last_error or "management_in_progress", "buildId": self.execution.application_build_id if self.execution else None}).encode()
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start_maintenance(self, *, backend: bool = True) -> None:
        if self.maintenance or self.config is None:
            return
        try:
            if backend:self.maintenance.append(await asyncio.start_server(self._maintenance_connection, "127.0.0.1", 8000, limit=4097))
            self.maintenance.append(await asyncio.start_server(self._maintenance_connection, self.config.web_bind, self.config.web_port, limit=4097))
        except BaseException:
            await self.close_maintenance()
            raise

    async def close_maintenance(self) -> None:
        for server in self.maintenance:
            server.close()
        for server in self.maintenance:
            await server.wait_closed()
        self.maintenance.clear()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        def stop():
            self.launches_open = False
            loop.call_soon_threadsafe(self.stopping.set)
        with immediate_stop_signals(stop):
            await self.run_owned()

    async def run_owned(self) -> None:
        try:
            with extending_startup(notify, lambda: self.launches_open) as deadline:
                notify("STATUS=StreamHome is verifying its selected runtime")
                await self.server.start()
                resumed = self.initial_resumption()
                if resumed is None:
                    execution = read_active_execution(ROOT).execution
                    journal = None
                else:
                    journal, self.resumption_binding = resumed
                    if isinstance(journal, dict):
                        if journal.get('managed_update_version') == 1:
                            from ..update_execution import parse as parse_update
                            execution = parse_update(journal).verification
                        else:
                            from ..parent_execution import parse_parent_execution
                            execution = parse_parent_execution(journal['managed_execution']).execution
                    else:
                        execution = contract.selected_execution(journal)
                self.execution, self.config = execution, self._deployment(execution)
                try:
                    async with self.lock:
                        if journal is None:
                            await self.launch(execution)
                        elif isinstance(journal, dict):
                            if journal.get('managed_update_version') == 1:
                                update = self._update_journal(journal['transaction_id'], journal['lifecycle_generation'])
                                await self.launch(execution, update=update, authorize=lambda:self._update_journal(
                                    journal['transaction_id'], journal['lifecycle_generation']))
                            else:
                                from ..parent_execution import parse_parent_execution
                                await self.launch(execution, restore=(journal,parse_parent_execution(journal['managed_execution'])),
                                    backend_only=True, authorize=lambda:self._parent_journal(journal['operation_id'],journal['generation']))
                                await self.start_maintenance(backend=False)
                        else:
                            await self.launch(execution, journal=journal,
                                              authorize=lambda: self.initial_resumption()[0])
                except Exception as exc:
                    self.last_error = exc.code if isinstance(exc, ControlError) else "control_runtime_unavailable"
                    await self.stop_children()
                    if not self.launches_open:
                        return
                    if self.last_error != "control_pending_deployment":
                        raise
                    # The root startup observer was scheduled before ExecStart;
                    # it can claim the exact saved deployment while this unit
                    # remains activating. No early READY breaks the cycle.
                    await self.start_maintenance()
                ready_kind = await self.wait_initial_readiness(deadline)
                if ready_kind is None:
                    return
                notify("READY=1\nSTATUS=StreamHome " + ready_kind + " runtime is ready")
            await self.parent_handoff.start()
            state._atomic_write_json(self.layout.runtime / "supervisor.json", self.snapshot())
            while not self.stopping.is_set():
                await asyncio.sleep(0.25)
                async with self.lock:
                    self.require_resumption()
                    self._remember_backend_socket()
                    authority = state.inspect_state(ROOT)
                    operation = authority.active_operation
                    if operation is not None and operation.operation_type is state.OperationType.RESTORE:
                        from ..managed_restore import fence_dead_controller
                        if fence_dead_controller(ROOT, authorize=self._parent_journal):
                            self._parent_journal(operation.operation_id, authority.generation,
                                phase=state.LifecyclePhase.RECOVERY_REQUIRED)
                            self.last_error = 'control_restore_controller_exited'
                            await self.stop_children()
                            await self.start_maintenance()
                            state._atomic_write_json(self.layout.runtime / 'supervisor.json', self.snapshot())
                    elif operation is not None and operation.operation_type is state.OperationType.UPDATE:
                        from ..managed_update import fence_dead_controller, failed_controller_is_quiescent
                        if (fence_dead_controller(ROOT, authorize=lambda operation,generation:self._update_journal(
                                    operation,generation,fence_only=True))
                                or self.children and failed_controller_is_quiescent(ROOT,
                                    authorize=lambda operation, generation: self._update_journal(
                                        operation, generation, fence_only=True))):
                            self.last_error = 'control_update_controller_exited'
                            await self.stop_children()
                            await self.start_maintenance()
                            state._atomic_write_json(self.layout.runtime / 'supervisor.json', self.snapshot())
                    # Popen.poll would reap an exited leader before descendant
                    # cleanup. WNOWAIT observes death while retaining identity.
                    dead = any(os.waitid(os.P_PID, child.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None for child in self.children.values())
                    if dead:
                        self.last_error = "control_runtime_exited"
                        await self.stop_children()
                        await self.start_maintenance()
                        state._atomic_write_json(self.layout.runtime / "supervisor.json", self.snapshot())
        finally:
            self.launches_open = False
            await self.parent_handoff.close()
            await self.server.close()
            await self.close_maintenance()
            await self.stop_children()
            notify("STOPPING=1\nSTATUS=StreamHome runtime stopped")

    def restore_transport_ready(self) -> bool:
        return self.parent_transport_ready('restore_verify', 'restore_transport_readiness')

    async def wait_update_guarded(self, peer: Peer, arguments: dict) -> None:
        """A start reply requires exact guarded backend and web readiness.

        The caller holds the supervisor lock. Do not call the initial-start
        waiter, which acquires that lock itself. Immediate stop signals still
        close launches while the owned processes complete guarded startup.
        """
        deadline = time.monotonic()+STARTUP_RECOVERY_SECONDS
        while self.launches_open:
            self._update_controller(peer, arguments)
            if time.monotonic() >= deadline:
                raise ControlError('control_startup_recovery_timeout')
            if (set(self.children) != {'backend', 'web'} or any(os.waitid(os.P_PID,
                    child.process.pid, os.WEXITED|os.WNOHANG|os.WNOWAIT) is not None
                    for child in self.children.values())):
                raise ControlError('control_runtime_exited')
            try:
                kind = await asyncio.to_thread(self.selected_readiness, guarded_update=True)
            except ControlError as exc:
                if exc.code not in {'control_unavailable', 'control_reply_incomplete', 'control_timeout'}:
                    raise
                kind = None
            except (OSError, ValueError, urllib.error.URLError):
                kind = None
            self._update_controller(peer, arguments)
            if not self.launches_open:
                break
            if kind == 'guarded':
                return
            await asyncio.sleep(.2)
        raise ControlError('control_operator_stopped')

    def update_transport_ready(self) -> bool:
        return self.parent_transport_ready('update_verify', 'update_transport_readiness')

    def parent_transport_ready(self, mode: str, action: str) -> bool:
        """Prove owned transport recovery before canonical completion/admission."""
        backend,web = self.children['backend'],self.children['web']
        for child in (backend,web):
            if state._process_start_identity(child.process.pid) != child.start:
                raise ControlError('control_process_changed')
        result = request(self.layout.runtime/'backend.sock', expected_uid=os.geteuid(),
            expected_pid=backend.process.pid, installation_id=self.installation, invocation=self.invocation,
            action=action,arguments={},timeout=2)
        if (set(result) != {'pid','start_identity','execution_sha256','readiness'}
                or result['pid'] != backend.process.pid or result['start_identity'] != backend.start
                or result['execution_sha256'] != contract.execution_tuple_sha256(self.execution)
                or not isinstance(result['readiness'],dict)):
            raise ControlError('control_runtime_identity_changed')
        readiness = result['readiness']
        if readiness.get('errorCode') not in {None,'runtime_not_settled'}:
            raise ControlError('control_startup_recovery_failed')
        if (readiness.get('mode') != mode or not all(readiness.get(name) is True
                for name in ('workersStarted','recoveryCompleted','recoverySucceeded'))):
            return False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{self.config.web_port}/api/health/live',timeout=2) as response:
            raw = response.read(32769)
            if len(raw)>32768:raise ControlError('control_health_invalid')
            value = json.loads(raw)
            if (not isinstance(value,dict) or response.headers.get('X-StreamHome-Web-Build') != self.execution.application_build_id
                    or value.get('buildId') != self.execution.application_build_id
                    or value.get('mode') != mode):
                raise ControlError('control_web_identity_changed')
            return all(value.get(name) is True for name in ('workersStarted','recoveryCompleted','recoverySucceeded'))

    def restore_guarded_readiness(self) -> str | None:
        """Release only systemd's start wait while canonical restore stays fenced."""
        binding = self.resumption_binding
        journal, bound = self._parent_journal(binding.operation_id, binding.generation,
            phase=state.LifecyclePhase.VERIFYING)
        backend = self.children.get('backend')
        if (set(self.children) != {'backend'} or backend is None
                or state._process_start_identity(backend.process.pid) != backend.start):
            raise ControlError('control_process_changed')
        result = request(self.layout.runtime/'backend.sock', expected_uid=os.geteuid(),
            expected_pid=backend.process.pid, installation_id=self.installation, invocation=self.invocation,
            action='restore_guarded_readiness', arguments={}, timeout=2)
        if (set(result) != {'pid','start_identity','execution_sha256','readiness'}
                or result['pid'] != backend.process.pid or result['start_identity'] != backend.start
                or result['execution_sha256'] != contract.execution_tuple_sha256(bound.execution)
                or not isinstance(result['readiness'],dict)):
            raise ControlError('control_runtime_identity_changed')
        projection = result['readiness']
        if projection.get('errorCode') not in {None,'runtime_not_settled'}:
            raise ControlError('control_startup_recovery_failed')
        if (projection.get('mode') != 'restore_verify' or projection.get('guardedReady') is not True
                or projection.get('workersStarted') is not False):
            return None
        self._parent_journal(binding.operation_id,binding.generation,phase=state.LifecyclePhase.VERIFYING)
        return 'guarded'

    def selected_readiness(self, *, guarded_update: bool = False) -> str | None:
        self.require_resumption()
        from .parent_resumption_runtime import ParentRuntimeBinding
        if isinstance(getattr(self, 'resumption_binding', None), ParentRuntimeBinding):
            return self.restore_guarded_readiness()
        from .update_resumption_runtime import UpdateRuntimeBinding
        guarded_update = guarded_update or isinstance(getattr(self, 'resumption_binding', None), UpdateRuntimeBinding)
        backend, web = self.children.get("backend"), self.children.get("web")
        if backend is None or web is None or self.execution is None or self.config is None:
            return None
        if (state._process_start_identity(backend.process.pid) != backend.start
                or state._process_start_identity(web.process.pid) != web.start):
            raise ControlError("control_process_changed")
        result = request(self.layout.runtime / "backend.sock", expected_uid=os.geteuid(),
            expected_pid=backend.process.pid, installation_id=self.installation, invocation=self.invocation,
            action='update_guarded_readiness' if guarded_update else 'readiness', arguments={}, timeout=2)
        if (set(result) != {"pid", "start_identity", "execution_sha256", "readiness"}
                or result["pid"] != backend.process.pid or result["start_identity"] != backend.start
                or result["execution_sha256"] != contract.execution_tuple_sha256(self.execution)
                or not isinstance(result["readiness"], dict)):
            raise ControlError("control_runtime_identity_changed")
        projection = result["readiness"]
        if guarded_update and projection.get('mode') != 'update_verify':
            raise ControlError('control_authority_changed')
        if projection.get("errorCode") not in (None, "runtime_not_settled"):
            raise ControlError("control_startup_recovery_failed")
        kind = ("general" if projection.get("generalReady") is True and projection.get("workersStarted") is True
                else "guarded" if projection.get("guardedReady") is True and projection.get("workersStarted") is False else None)
        if kind is None or projection.get("recoverySucceeded") is not True or projection.get("recoveryCompleted") is not True:
            return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.config.web_port}/api/health", timeout=2) as response:
            raw = response.read(32769)
            if len(raw) > 32768:
                raise ControlError("control_health_invalid")
            value = json.loads(raw)
            if (not isinstance(value, dict) or response.headers.get("X-StreamHome-Web-Build") != self.execution.application_build_id
                    or value.get("buildId") != self.execution.application_build_id or value.get("readyKind") != kind):
                raise ControlError("control_web_identity_changed")
        return kind

    async def wait_initial_readiness(self, deadline: float) -> str | None:
        while self.launches_open and not self.stopping.is_set():
            if time.monotonic() >= deadline:
                raise ControlError("control_startup_recovery_timeout")
            async with self.lock:
                self.require_resumption()
                if any(os.waitid(os.P_PID, child.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None for child in self.children.values()):
                    raise ControlError("control_runtime_exited")
                try:
                    kind = await asyncio.to_thread(self.selected_readiness)
                except ControlError as exc:
                    if exc.code not in {"control_unavailable", "control_reply_incomplete", "control_timeout"}:
                        raise
                    kind = None
                except (OSError, ValueError, urllib.error.URLError):
                    kind = None
                if not self.launches_open:
                    return None
                if kind is not None:
                    return kind
            notify("STATUS=StreamHome is completing mandatory startup recovery")
            await asyncio.sleep(0.25)
        return None


def main(*, control_bundle_id: str, control_manifest_sha256: str) -> int:
    asyncio.run(Supervisor(control_bundle_id=control_bundle_id, control_manifest_sha256=control_manifest_sha256).run())
    return 0
