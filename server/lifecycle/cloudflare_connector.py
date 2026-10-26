"""Protected immutable references to existing, owner-supplied Cloudflare tunnels.

The deployment revision owns origin/listener/proxy policy. A connector reference
identifies credentials and a pinned isolated binary; it grants no application
authority and never proves provider connectivity or public reachability.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import uuid

from .deployment_config import AccessMode,DeploymentConfig
from .control import entry
from .control.rpc import ControlError


REGISTRY=Path('/etc/streamhome/control/cloudflare/connectors')
BINARY_ROOT=Path('/usr/local/libexec/streamhome/cloudflared')
VERSION='2026.10.3'
SHA256='053b80b0a23031a038fdbf3c073f59f82b8f1f152aa67971d7383ffafc0dbab0'
DOWNLOAD='https://github.com/cloudflare/cloudflared/releases/download/'+VERSION+'/cloudflared-linux-amd64'


def identity(value: str) -> str:
    try:parsed=uuid.UUID(value) if isinstance(value,str) else None
    except ValueError as exc:raise ControlError('control_tunnel_reference_invalid') from exc
    if parsed is None or not parsed.int or str(parsed)!=value:raise ControlError('control_tunnel_reference_invalid')
    return value


@dataclass(frozen=True)
class Connector:
    connector_id: str
    label: str
    binary_version: str=VERSION
    binary_sha256: str=SHA256

    def payload(self):
        return {'schema_version':1,'connector_id':self.connector_id,'label':self.label,
            'binary_version':self.binary_version,'binary_sha256':self.binary_sha256}

    @property
    def unit(self):return 'streamhome-tunnel@'+self.connector_id+'.service'

    @property
    def binary(self):return BINARY_ROOT/self.binary_version/'cloudflared'


def parse(value: dict) -> Connector:
    if (not isinstance(value,dict) or set(value)!={'schema_version','connector_id','label','binary_version','binary_sha256'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or not isinstance(value['label'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}',value['label'])
            or value['binary_version']!=VERSION or value['binary_sha256']!=SHA256):
        raise ControlError('control_tunnel_metadata_invalid')
    return Connector(identity(value['connector_id']),value['label'])


def _directory(path):
    current=Path('/')
    entry._protected(current,directory=True)
    for part in path.parts[1:]:
        current/=part;entry._protected(current,directory=True)


def read(connector_id: str) -> Connector:
    path=REGISTRY/identity(connector_id)
    try:
        _directory(path)
        value=parse(entry._decode(entry._read(path/'metadata.json',8192)))
    except (OSError,entry.EntryError,ValueError) as exc:
        raise ControlError('control_tunnel_reference_unavailable') from exc
    if value.connector_id!=connector_id:raise ControlError('control_tunnel_reference_invalid')
    return value


def inventory() -> tuple[Connector,...]:
    try:REGISTRY.lstat()
    except FileNotFoundError:return ()
    try:
        _directory(REGISTRY)
        names=sorted(path.name for path in REGISTRY.iterdir())
    except (OSError,entry.EntryError) as exc:raise ControlError('control_tunnel_registry_untrusted') from exc
    if len(names)>64:raise ControlError('control_tunnel_registry_full')
    return tuple(read(name) for name in names)


def require_deployment(config: DeploymentConfig) -> Connector | None:
    if config.access_mode is not AccessMode.CLOUDFLARE:return None
    return read(config.cloudflare_connector_id)
