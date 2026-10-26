# Installing StreamHome

These instructions describe the managed Linux installer in this source tree.
Older checkout installations retain their existing scripts and data layout; they
are not automatically adopted or moved by the managed installer.

## Supported server

The qualified managed target is Ubuntu 24.04 x86_64 with systemd, cgroup v2 and
the distribution Python 3.12.3 interpreter. Use a local Linux filesystem with
working file locks, atomic replacement and file/directory fsync. Network and FUSE
filesystems are unsupported for canonical lifecycle state and the database.
Windows, containers, NAS packages and other Linux distributions are not qualified
managed-installation targets.

Install as an ordinary account with sudo access and internet connectivity:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The frontend stays unprivileged. It verifies the publisher-signed independent
control package before requesting administrative actions. Do not run a writable
checkout script as root. To inspect the frontend first, download it from the
official repository, read it and run it with `bash install.sh`.

The wizard reads the controlling terminal even with `curl | bash`. It asks for the
web port, access mode, related endpoint settings, start-at-boot policy and final
installation approval. Pressing Enter at `[Y/n]` selects Yes. Use the displayed
back/cancel choices to revise a draft. Without a controlling terminal, supply a
complete explicit JSON request with `--request FILE`; missing answers are not
silently inferred.

Preparation reports each owned stage, including application Node.js, Python/web
dependencies, web build, Rclone and complete runtime verification. StreamHome uses
its own pinned Node/npm environment; an incompatible global Node/npm installation
does not need to be replaced. Preserve the recorded installation intent if a
stage fails and follow the diagnostic command printed by the installer.

## Fixed installation paths

The installer does not ask for a destination.

| Path | Purpose |
| --- | --- |
| `/opt/streamhome` | Application source and private runtime generations |
| `/etc/streamhome/application` | Application configuration and secrets |
| `/etc/streamhome/domains` | Versioned deployment configuration |
| `/etc/streamhome/install.json` | Protected installation identity |
| `/var/lib/streamhome/database.db` | Canonical SQLite database |
| `/var/lib/streamhome/backup` | Local database backups |
| `/var/lib/streamhome/lifecycle` | Durable operation and recovery evidence |
| `/var/cache/streamhome` | Disposable preparation and verified recovery inputs |
| `/run/streamhome` | Private live process/control endpoints |
| `/usr/local/bin/streamhome` | Global management command |
| `/usr/local/libexec/streamhome/provision` | Protected provisioning/rescue entry |

A dedicated `streamhome` account owns application runtime work. Media remains
under application storage configuration; this installer does not relocate an
existing library. Preserve configuration, database, media and recovery evidence
when changing or repairing an installation.

## Help, service control and setup

The command works from any directory. With no arguments it displays help:

```bash
streamhome
sudo streamhome status
sudo streamhome version
sudo streamhome doctor
sudo streamhome start
sudo streamhome stop
sudo streamhome logs --lines 100
```

The corresponding service is `streamhome.service`:

```bash
sudo systemctl status streamhome.service
sudo systemctl start streamhome.service
sudo systemctl stop streamhome.service
```

Retrieve the private one-time setup code from a controlling terminal:

```bash
sudo streamhome bootstrap
# Alias for the same code:
sudo streamhome setup-code
```

Enter it only on this installation's `/setup` page. It expires after 15 minutes
and can be consumed once. Use `--renew` explicitly if a new code is needed. Setup
creates the administrator and configures optional local TOTP, metadata access and
storage. StreamHome does not use SMTP or email OTP. Keep passwords, codes, TOTP
secrets, OAuth credentials, Rclone configuration and session cookies private.

## Ports, domains and boot policy

```bash
sudo streamhome configure
```

The configuration wizard reviews the current and pending settings, validates the
endpoint and listener, and distinguishes saving a deployment from applying it.
Saving changes does not mean they are already active. Review the proposed
interruption before applying changes to a running service. Configure start-at-boot
through the same reviewed workflow.

The default local web endpoint is `http://localhost:3000`; the API remains on
`127.0.0.1:8000`. Managed web ports are 1024–65535 except 8000. LAN mode exposes the
web listener and requires a matching LAN address or hostname. HTTPS proxy mode
keeps the application behind the reviewed proxy and derives allowed origins and
cookie behavior from the public origin. Do not expose the API directly.

Cloudflare mode connects an existing remotely managed tunnel. The wizard can
register its token from a private regular file, or select an existing protected
connector reference. Registration happens after final confirmation. To prepare
an inactive connector separately:

```bash
sudo streamhome tunnel add --token-file /root/streamhome-tunnel.token --name "Media tunnel"
sudo streamhome tunnel list
sudo streamhome configure
sudo streamhome tunnel status
```

Keep the input token file private (`0600`). StreamHome stores its copy as a
root-only file and delivers it through systemd credentials to a separate dynamic
service user. Tokens never appear in command arguments or configuration reviews.
The connector uses a pinned application-owned cloudflared executable and leaves
other cloudflared installations and services alone.

In Cloudflare's tunnel configuration, route the chosen public hostname to
`http://127.0.0.1:PORT`, using the web port reviewed in StreamHome. Enter that
hostname's HTTPS origin in the wizard. StreamHome applies the corresponding
origin and proxy policy; tunnel creation and provider hostname/DNS routing remain
provider-side steps. Accepted configuration selects the connector; saved pending
settings do not switch it. Stopping StreamHome also stops its connector. A local
health check or an active connector service does not prove public reachability.

## Existing installations and updates

Running the installer again detects existing installation evidence, reports its
state, checks release eligibility when available and exits. It does not reinstall,
apply an update or move a legacy installation. Use the application's supported
Updates workflow for eligible application updates. Back up durable data first.
A failed pre-acceptance activation may restore its exact previous release and
bound database checkpoint. Accepted changes require forward recovery and are
never automatically rolled back.

Global `streamhome update`, `streamhome restart`, uninstall and reset commands are
not provided by this scope. Do not use rerunning the installer as an uninstall,
reset or forced-update mechanism.

## Diagnostics and interrupted work

Start with `sudo streamhome doctor` and the exact operation identity reported by
StreamHome. Read the current recovery review before authorizing continuation:

```bash
sudo streamhome recover --operation OPERATION_UUID --review
```

Available actions depend on current, validated evidence. Recovery can require a
fresh review and explicit service-resumption approval; a stopped service is not
permission to launch it automatically. Exact dependency repair uses
`sudo streamhome repair --operation OPERATION_UUID --review` when offered. Repair
preserves the original unresolved operation and does not itself authorize a start.
Use the independently verified protected rescue path if control files are damaged;
do not replace root-owned control modules from a writable checkout.

For release development, use isolated test installations. `test.sh` can initialize
state and replace frontend build output, so it is not a read-only production
health command. A ready `/api/health` response through the web proxy, successful
sign-in and storage checks verify the corresponding live installation only.

## Related documentation

- [Getting Started](getting-started.md)
- [Initial Setup](setup.md)
- [Security](security.md)
- [Google Drive Integration](google-drive.md)
- [Backup and Recovery](backup-and-recovery.md)
- [Troubleshooting](troubleshooting.md)
