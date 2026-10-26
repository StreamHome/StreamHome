# Managed installation identity contract

The managed installer, protected global command, fixed layout and foreground
systemd supervisor are integrated in this source tree. Runtime preparation uses
application-owned pinned dependencies and exact immutable execution tuples.
[Installation](../installation.md) documents current operator entry points;
[configuration contracts](configuration-management.md), [management authority](management-protocol.md)
and [recovery](management-recovery.md) describe the trust and lifecycle boundaries.

This contract does not establish publication or deployment status. Root-control
maintenance, provider integration and operation-specific native qualification must
meet their own release gates. A descriptor alone is never mutation or launch authority.

## Existing Cloudflare connectors

Control support schema 13 advertises `cloudflare_connector_versions: [1]`.
The optional `streamhome-tunnel@.service` and
`streamhome-tunnel-reconcile.service` stay outside the unchanged six-unit core
protocol. Registration publishes an immutable UUID reference and a root-only
token after final consent. The pinned vendor executable is hash-verified before
publication; no system cloudflared executable or account-wide service is adopted.

Canonical deployment revisions own connector selection, port, origin and proxy
policy. Registration alone does not alter provider revisions or start a service.
The root reconciler consumes the UID's verified active settings and admitted
backend plus exact OS invocation; it never parses canonical state as root.
Write-ahead selection metadata permits an interrupted switch to stop only the
previous protected StreamHome connector. Fixed unit bytes and loaded properties
are checked before effects. Requisite/PartOf prevent a connector from starting
the primary application and stop it with that application. A dynamic service UID
receives its token through systemd credentials.

Startup queues reconciliation without blocking the primary startup job. Waiting
for canonical acceptance happens outside the administrator lock and is bounded;
provider health never certifies application readiness. Read-only CLI commands
never schedule reconciliation. Real tunnel/DNS/public authentication checks are
a separate operator-server qualification gate.

## Descriptor version 1

The protected descriptor is `/etc/streamhome/install.json`. Its complete schema is:

```json
{
  "schema_version": 1,
  "layout_version": 1,
  "installation_id": "c43ae73f-c899-4ab5-a71b-c66639245104",
  "status": "provisioning",
  "service_uid": 1001,
  "service_gid": 1001,
  "paths": {
    "application": "/opt/streamhome",
    "configuration": "/etc/streamhome",
    "state": "/var/lib/streamhome"
  }
}
```

The UUID and service IDs above are examples. The provisioner must allocate and
verify the real installation identity and dedicated OS account. This reader
validates representation; it does not prove that the account exists or grant it
privileged access. Extra/missing/duplicate fields, nonstandard JSON numbers,
unsupported versions, root service IDs and noncanonical UUIDs are rejected.
`status` is exactly `provisioning` or `ready`. There are no credentials or media
locations in this document.

Paths are fixed, not destination overrides. Derived paths are
`/var/lib/streamhome/database.db`, `/var/lib/streamhome/lifecycle`,
`/var/cache/streamhome`, `/run/streamhome`, `/usr/local/libexec/streamhome` and
`/usr/local/bin/streamhome`. Integrating those paths into normal/offline consumers
requires its own completed lifecycle transition; parsing a descriptor alone never
selects or creates a database. Media paths remain owned by application configuration.

## Read trust and consistency

The production reader walks `/etc/streamhome` with directory file descriptors,
rejecting symlinks, non-root owners and group/world-writable components. It opens
the descriptor without following links or blocking on a FIFO. The descriptor
must be one regular root-owned file, without executable/special permission bits,
extra hard links or group/world write permission. Reads are bounded to 8192 bytes.

File metadata and directory-entry identity are rechecked after reading. Detected
replacement or mutation returns `descriptor_changed`; errors never fall back to
an empty installation. A returned SHA-256 identifies the inspected bytes for
later comparison, not publisher authentication or lifecycle ownership. Privileged
effects will still need their current canonical authority and fresh evidence.

`read_descriptor_at` accepts an already trusted directory FD. Its owner override
supports unprivileged disposable tests. Production entry points keep root-owner
validation and provide their own protected directory; they must not accept an
untrusted directory FD or owner override from a management request.

## Read-only developer inspection

From the checkout, with Python 3.11 or newer:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server python3 -m lifecycle.installation
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server python3 -m lifecycle.installation inspect
```

The first command displays help without reading installation state. Inspection
prints JSON and accepts no path, environment or ownership override.

| Exit | Meaning |
| --- | --- |
| 0 | Help, or a validated ready descriptor |
| 2 | Invalid, unsupported, untrusted or changed evidence |
| 3 | Validated provisioning descriptor; ordinary managed start remains ineligible |
| 4 | Managed descriptor absent; legacy discovery is still required |

Absence is never proof that installing another copy is safe. These commands
perform no registration, adoption, migrations, dependency preparation or service
control. A ready descriptor alone is not proof of health, authentication, setup
eligibility or idle lifecycle authority.

## Managed port policy

Installed `streamhome configure` distinguishes an occupied listener from TCP
TIME_WAIT reservations left by closing connections. For TIME_WAIT it reports a
wait with a 65-second retry deadline. Each read-only inspection retains its
30-second process timeout; no retry continues after the deadline is observed. Every
attempt uses the same bind/listen probe without `SO_REUSEADDR` or `SO_REUSEPORT`.
It never treats TIME_WAIT as proof of ownership, signals another process or
bypasses a bound, non-listening reservation. A stop request cancels the wait;
authority and configuration remain unchanged until preflight and review finish.
Persistent conflicts and expired waits produce separate actionable diagnostics.

`validate_managed_port` accepts integer or ASCII decimal input, returns a port in
1024–65535, and excludes backend port 8000. It rejects booleans, fractional,
signed, whitespace-padded and Unicode-digit input. Leading-zero decimal strings
are interpreted in base ten. Listener ownership, availability and exact-build
readiness must still be verified by activation. Legacy low-port settings are
not rewritten by this validator.
