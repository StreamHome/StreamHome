"""Root-only inactive connector preparation; canonical deployment owns selection."""
from __future__ import annotations

from dataclasses import dataclass,field
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.request
import uuid

from .. import cloudflare_connector as metadata
from ..terminal import Progress
from . import cloudflare_units,entry
from .privileged import require_root,systemctl
from .provisioning import administrative_lock
from .root_files import atomic_root_bytes,atomic_root_json,root_directory
from .rpc import ControlError
from .support import require_supported_platform


ACTIVE=Path('/etc/streamhome/control/cloudflare/active.json')
RECONCILE='streamhome-tunnel-reconcile.service'


@dataclass(frozen=True)
class DraftConnector:
    connector: metadata.Connector
    token: bytes=field(repr=False)


def require_capability(manifest):
    require_root()
    support=manifest.get('support',{})
    versions=support.get('cloudflare_connector_versions')
    if (type(support.get('schema_version')) is not int or support['schema_version']<13
            or versions!=[1] or any(type(value) is not int for value in versions)):
        raise ControlError('control_tunnel_unavailable')


def draft(token_file: Path,label: str,*,manifest: dict) -> DraftConnector:
    """Read bounded secret data into RAM; no filesystem/service effect."""
    require_capability(manifest);require_supported_platform()
    connector=metadata.parse(metadata.Connector(str(uuid.uuid4()),label).payload())
    descriptor=os.open(token_file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(descriptor,'rb') as source:
        before=os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_mode&0o7177
                or before.st_size>8192):raise ControlError('control_tunnel_token_file_untrusted')
        value=source.read(8193)
        after=os.fstat(source.fileno());linked=token_file.lstat()
        identity=lambda item:(item.st_dev,item.st_ino,item.st_uid,item.st_mode,item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if identity(before)!=identity(after) or identity(before)!=identity(linked):
            raise ControlError('control_tunnel_token_file_changed')
    return DraftConnector(connector,_token(value.rstrip(b'\r\n')))


def _token(value):
    if not isinstance(value,bytes) or not 32<=len(value)<=8192 or not re.fullmatch(rb'[A-Za-z0-9_+/=-]+',value):
        raise ControlError('control_tunnel_token_invalid')
    return value


def _binary_verified(path: Path) -> None:
    root_directory_fd=root_directory(path.parent)
    os.close(root_directory_fd)
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(descriptor,'rb') as source:
        before=os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1
                or stat.S_IMODE(before.st_mode)!=0o755 or not 1024<=before.st_size<=128*1024*1024):
            raise ControlError('control_tunnel_binary_untrusted')
        digest=hashlib.sha256()
        while block:=source.read(1024*1024):digest.update(block)
        after=os.fstat(source.fileno());linked=path.lstat()
        identity=lambda item:(item.st_dev,item.st_ino,item.st_uid,item.st_mode,item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if identity(before)!=identity(after) or identity(before)!=identity(linked) or digest.hexdigest()!=metadata.SHA256:
            raise ControlError('control_tunnel_binary_untrusted')


def ensure_binary() -> None:
    """Install only the pinned standalone executable; preserve existing damage."""
    require_root()
    target=metadata.BINARY_ROOT/metadata.VERSION/'cloudflared'
    if target.exists() or target.is_symlink():
        _binary_verified(target);return
    parent=root_directory(target.parent,create=True)
    temporary='.download-'+uuid.uuid4().hex
    descriptor=None
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
        digest=hashlib.sha256();total=0;deadline=time.monotonic()+180
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request=urllib.request.Request(metadata.DOWNLOAD,headers={'User-Agent':'StreamHome','Accept':'application/octet-stream'})
        with os.fdopen(descriptor,'wb') as output:
            descriptor=None
            with opener.open(request,timeout=20) as response:
                if response.status!=200 or not response.url.startswith('https://'):
                    raise ControlError('control_tunnel_download_failed')
                while block:=response.read(1024*1024):
                    total+=len(block)
                    if total>128*1024*1024 or time.monotonic()>=deadline:raise ControlError('control_tunnel_download_failed')
                    output.write(block);digest.update(block)
            if total<1024 or digest.hexdigest()!=metadata.SHA256:raise ControlError('control_tunnel_download_untrusted')
            os.fchmod(output.fileno(),0o755);output.flush();os.fsync(output.fileno())
        if target.exists() or target.is_symlink():raise ControlError('control_tunnel_binary_changed')
        os.rename(temporary,target.name,src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
        _binary_verified(target)
    finally:
        if descriptor is not None:os.close(descriptor)
        os.close(parent)
        # Incomplete downloads are intentionally retained as inert root-owned
        # evidence; a retry cannot execute or silently adopt them.


def ensure_units() -> None:
    require_root();changed=False
    for name,raw in cloudflare_units.unit_files().items():
        path=Path(name);parent=root_directory(path.parent)
        os.close(parent)
        if path.exists() or path.is_symlink():
            if entry._read(path,16384)!=raw:raise ControlError('control_tunnel_unit_untrusted')
        else:
            atomic_root_bytes(path,raw);changed=True
    if changed:systemctl('daemon-reload',timeout=30)


def register(value: DraftConnector,*,manifest: dict) -> metadata.Connector:
    """Publish an approved inactive reference; never start or enable a tunnel."""
    require_capability(manifest);require_supported_platform()
    if type(value) is not DraftConnector or metadata.parse(value.connector.payload())!=value.connector:
        raise ControlError('control_tunnel_metadata_invalid')
    _token(value.token)
    with administrative_lock(reuse=True):
        if len(metadata.inventory())>=64:raise ControlError('control_tunnel_registry_full')
        ensure_binary();ensure_units()
        parent=root_directory(metadata.REGISTRY,create=True)
        pending=metadata.REGISTRY.parent/'pending'/value.connector.connector_id
        descriptor=root_directory(pending,create=True)
        os.close(descriptor)
        try:
            destination=metadata.REGISTRY/value.connector.connector_id
            if destination.exists() or destination.is_symlink():raise ControlError('control_tunnel_reference_changed')
            for name in ('token','metadata.json'):
                if (pending/name).exists() or (pending/name).is_symlink():raise ControlError('control_tunnel_reference_changed')
            atomic_root_bytes(pending/'token',value.token,mode=0o600)
            atomic_root_json(pending/'metadata.json',value.connector.payload(),mode=0o644)
            os.rename(pending,destination);os.fsync(parent)
            metadata.read(value.connector.connector_id)
        finally:os.close(parent)
    return value.connector


def wizard_options(manifest,pending):
    """Return read-only draft hooks; no registration/download before consent."""
    support=manifest.get('support',{})
    if support.get('cloudflare_connector_versions')!=[1]:return (),None
    require_capability(manifest)
    references=[value.connector_id for value in metadata.inventory()]
    def stage(console):
        from ..configure_wizard import BackToMenu
        def label(value):
            if value.lower()=='back':raise BackToMenu()
            try:return metadata.parse(metadata.Connector(str(uuid.uuid4()),value).payload()).label
            except ControlError as exc:raise ValueError('Use a short tunnel label with letters, numbers, spaces, dots, underscores or hyphens.') from exc
        name=console.value('Existing tunnel label (or back)','Existing tunnel',label)
        def read(value):
            if value.lower()=='back':raise BackToMenu()
            if not value:raise ValueError('Enter the path to the existing tunnel token file.')
            try:return draft(Path(value),name,manifest=manifest)
            except (ControlError,OSError) as exc:raise ValueError('The token file must contain one valid token in a private regular file.') from exc
        value=console.value('Existing tunnel token file (or back)','',read)
        pending[value.connector.connector_id]=value
        references.append(value.connector.connector_id)
        console.write('This draft registers an isolated connector only after final confirmation; no tunnel is started yet.')
        return value.connector.connector_id
    return references,stage


def finalize_draft(config,pending,*,manifest):
    """Called only after the install/configure review and interruption consent."""
    value=pending.get(config.cloudflare_connector_id)
    if value is not None:
        with Progress(sys.stdout).stage('Preparing the isolated existing Cloudflare connector'):
            register(value,manifest=manifest)
    return metadata.require_deployment(config)


def _accepted_context():
    """Root consumes only independently verified UID and OS projections."""
    from ..deployment_config import parse_deployment_payload
    from .administration import process_snapshot,_sha,_verify_owned_units
    from .privileged import descriptor,run_worker,unit_snapshot
    selected=descriptor()
    if selected.status.value!='ready':return None
    unit=unit_snapshot()
    if unit['ActiveState'] not in {'active','activating'} or int(unit['MainPID'])<=1:return None
    _verify_owned_units()
    value=run_worker(['inspect'],timeout=120)
    if (value.get('runtime_verified') is not True or value['lifecycle']['active_operation'] is not None
            or value['installation_id']!=selected.installation_id):return None
    config=parse_deployment_payload(value['active_deployment'])
    metadata.require_deployment(config)
    processes=process_snapshot()
    if (processes['initial_service_state']!='running' or processes['backend'] is None
            or processes['supervisor_invocation_id']!=unit['InvocationID'] or unit_snapshot()!=unit):
        return None
    return {'installation_id':selected.installation_id,'generation':value['lifecycle']['generation'],
        'invocation':unit['InvocationID'],'execution_sha256':_sha(value['execution']),
        'deployment':config.payload(),'connector_id':config.cloudflare_connector_id}


def _active():
    try:ACTIVE.lstat()
    except FileNotFoundError:return None
    from .tickets import read_protected_json
    value=read_protected_json(ACTIVE,maximum=8192)
    fields={'schema_version','installation_id','generation','invocation','previous','selected','stage'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1
            or type(value['generation']) is not int or value['generation']<0
            or not isinstance(value['invocation'],str) or not re.fullmatch(r'[0-9a-f]{32}',value['invocation'])
            or not isinstance(value['previous'],list) or len(value['previous'])>64
            or not isinstance(value['stage'],str) or value['stage'] not in {'applying','selected'}):
        raise ControlError('control_tunnel_selection_untrusted')
    metadata.identity(value['installation_id'])
    if value['selected'] is not None:metadata.identity(value['selected'])
    for item in value['previous']:metadata.identity(item)
    if len(set(value['previous']))!=len(value['previous']) or value['selected'] in value['previous']:
        raise ControlError('control_tunnel_selection_untrusted')
    return value


def _owned_unit(connector):
    """Never stop or adopt the machine's unrelated cloudflared service."""
    for name,raw in cloudflare_units.unit_files().items():
        parent=root_directory(Path(name).parent);os.close(parent)
        if entry._read(Path(name),16384)!=raw:raise ControlError('control_tunnel_unit_untrusted')
    raw=systemctl('show',connector.unit,'--property=LoadState,FragmentPath,DropInPaths,NeedDaemonReload,User,DynamicUser')
    value=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    if value!={'LoadState':'loaded','FragmentPath':'/etc/systemd/system/streamhome-tunnel@.service',
            'DropInPaths':'','NeedDaemonReload':'no','User':'streamhome-tunnel','DynamicUser':'yes'}:
        raise ControlError('control_tunnel_unit_untrusted')


def reconcile(*,manifest):
    """Reflect accepted configuration; never start the application or use desired settings."""
    require_capability(manifest)
    # Startup may finish before the original controller publishes acceptance.
    # Wait outside the administrator lock so that controller/recovery can finish.
    from .privileged import unit_snapshot
    deadline=time.monotonic()+900
    while True:
        context=_accepted_context()
        if context is not None:break
        if unit_snapshot()['ActiveState'] not in {'active','activating'}:return 0
        if time.monotonic()>=deadline:raise ControlError('control_tunnel_acceptance_pending')
        time.sleep(1)
    with ExitStack() as locks:
        while True:
            try:
                locks.enter_context(administrative_lock())
                break
            except ControlError as exc:
                if exc.code!='control_administrator_busy' or time.monotonic()>=deadline:raise
                time.sleep(1)
        if _accepted_context()!=context:raise ControlError('control_tunnel_context_changed')
        chosen=context['connector_id'];previous=_active()
        if previous is not None and previous['installation_id']!=context['installation_id']:
            raise ControlError('control_tunnel_selection_untrusted')
        old=set(previous['previous']) if previous is not None else set()
        if previous is not None and previous['selected'] is not None:old.add(previous['selected'])
        old.discard(chosen)
        if len(old)>64:raise ControlError('control_tunnel_selection_untrusted')
        connectors={identity:metadata.read(identity) for identity in sorted(old|({chosen} if chosen else set()))}
        def live():
            if _accepted_context()!=context:raise ControlError('control_tunnel_context_changed')
        for connector in connectors.values():_owned_unit(connector)
        record={'schema_version':1,'installation_id':context['installation_id'],'generation':context['generation'],
            'invocation':context['invocation'],'previous':sorted(old),'selected':chosen,'stage':'applying'}
        live();atomic_root_json(ACTIVE,record)
        for identity in sorted(old):
            live();_owned_unit(connectors[identity]);systemctl('stop',connectors[identity].unit,timeout=30)
        if chosen is not None:
            connector=connectors[chosen]
            _binary_verified(connector.binary)
            token=metadata.REGISTRY/chosen/'token'
            observed=entry._protected(token)
            if stat.S_IMODE(observed.st_mode)!=0o600:raise ControlError('control_tunnel_token_file_untrusted')
            _token(entry._read(token,8192))
            live();_owned_unit(connector);systemctl('--no-block','start',connector.unit,timeout=15)
        live();atomic_root_json(ACTIVE,{**record,'previous':[],'stage':'selected'})
    return 0


def queue(*,manifest):
    """Best-effort post-acceptance follow-up; provider health never certifies app readiness."""
    if manifest.get('support',{}).get('cloudflare_connector_versions')!=[1]:return
    require_capability(manifest)
    if not metadata.REGISTRY.exists() and not ACTIVE.exists():return
    from .privileged import unit_snapshot
    if unit_snapshot()['ActiveState'] not in {'active','activating'}:return
    # Registration installs the fixed optional root dispatcher before a connector
    # can be selected. This command only queues that already protected unit.
    path=Path('/etc/systemd/system')/RECONCILE
    if entry._read(path,16384)!=cloudflare_units.RECONCILE_UNIT.encode():raise ControlError('control_tunnel_unit_untrusted')
    systemctl('--no-block','start',RECONCILE,timeout=15)


def queue_after_change(*,manifest):
    """An optional provider cannot change the primary action's success result."""
    try:queue(manifest=manifest)
    except (ControlError,entry.EntryError,OSError,ValueError,RuntimeError,subprocess.SubprocessError):
        # Deliberately omit exception detail: provider errors can contain private
        # data. The fixed unit journal and explicit tunnel status remain usable.
        print('StreamHome: tunnel reconciliation could not be queued; inspect sudo streamhome tunnel status.',file=sys.stderr)


def command(action,*,manifest,token_file=None,label='Existing tunnel'):
    require_capability(manifest)
    if action=='add':
        value=register(draft(token_file,label,manifest=manifest),manifest=manifest)
        print('Registered inactive connector: '+value.connector_id+' ('+value.label+')')
        print('Use sudo streamhome configure to review and activate its deployment settings.')
        return 0
    values=metadata.inventory()
    if not values:print('No StreamHome Cloudflare connectors are registered.')
    for value in values:
        print(value.connector_id+'  '+value.label)
        if action=='status':
            _owned_unit(value)
            print('  Service: '+systemctl('show',value.unit,'--property=ActiveState,SubState'))
    if action=='status':print('Service state does not prove tunnel routing, DNS or public reachability.')
    return 0
