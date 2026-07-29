# Installing StreamHome

StreamHome’s supported alpha server target is Linux. The repository provides one
general installer for the current `main` branch; there is no version-specific
download command.

> [!IMPORTANT]
> The alpha is intended for evaluation and testing. Back up any existing
> StreamHome data before updating, and put HTTPS in front of the web service
> before exposing it to the public internet.

## Quick installation

Run the official installer as the user who should own the StreamHome files:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The default installation directory is `~/StreamHome`.

The installer:

1. installs Git when it is missing and system-package installation is allowed;
2. acquires a per-installation lock;
3. fetches the current `main` branch;
4. promotes a new checkout atomically or safely updates an existing clean
   checkout;
5. verifies that the checkout exactly matches the fetched commit;
6. installs or verifies the Linux runtime dependencies;
7. creates the Python virtual environment and installs pinned server
   dependencies;
8. installs the pinned npm dependencies and builds the production web client;
9. installs an application-owned Rclone binary when the system version is
   unavailable or too old;
10. starts StreamHome and waits for both services to become healthy.

After a successful start, the terminal prints the setup URL and a one-time
bootstrap code. Keep that code private and enter it only on the matching
StreamHome `/setup` page.

## Review before running

To inspect the installer first:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh -o install.sh
less install.sh
bash install.sh
```

Only use scripts obtained from the official
`StreamHome/StreamHome` repository.

## Supported Linux environments

The alpha installer recognizes these package managers:

- `apt-get` on Debian and Ubuntu families;
- `dnf` on current Fedora and related distributions;
- `yum` on compatible distributions;
- `pacman` on Arch Linux and related distributions.

Other Linux distributions can be used only when all required commands are
installed manually and the installer is run with `--skip-system-packages`.
Windows server scripts, container images, Kubernetes manifests, NAS app-store
packages, and multi-node deployment are not release-gated for this alpha.

## Runtime requirements

The setup script requires:

- a 64-bit Linux server;
- Python 3.11 or newer;
- Node.js 18 or newer;
- npm;
- Git;
- FFmpeg and FFprobe;
- `curl`;
- `lsof` or another supported listener inspector;
- internet access while dependencies and application assets are installed.

A practical starting point is 2 CPU cores, 2 GB RAM, and 20 GB of free local
storage. High-bitrate playback, transcoding, large catalogs, and multiple
simultaneous viewers need more CPU, memory, storage, and network capacity.

## Installer options

### Choose an installation directory

```bash
STREAMHOME_INSTALL_DIR=/srv/streamhome \
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The destination must be an absolute or user-relative Linux path that the
installing user can write. Do not point it at an unrelated non-empty directory.

### Build without starting

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh \
  | bash -s -- --no-start
```

Start it later from the installation directory:

```bash
cd ~/StreamHome
./start.sh
```

### Skip operating-system package installation

Use this only after installing all runtime commands yourself:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh \
  | bash -s -- --skip-system-packages
```

The setup fails with a list of missing commands instead of changing system
packages.

## Updating an existing installation

Back up the database and configuration, then rerun the same general installation
command. Do not stop StreamHome first; the installer keeps the current release
online during isolated preparation and performs its own protected cutover:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The updater preserves installation-owned configuration and refuses to replace a
checkout with tracked local modifications. Commit, move, or otherwise reconcile
intentional source changes before updating. Generated build metadata and the
host-generated system profile are excluded from Git and do not block an update.
The installer returns only after the API and web client pass health checks; a
failed activation automatically restores the previous release and database
checkpoint.

## Starting and stopping

From the installation directory:

```bash
./start.sh
./stop.sh
```

`start.sh` refuses conflicting listeners, records the process IDs it owns, waits
for the API and web health checks, and prints the public setup or application
URL. If an update controller is active, it waits with phase reports and verifies
the controller's restored runtime instead of starting competing processes.
`stop.sh` terminates only StreamHome-owned processes, including the temporary
maintenance responder, and can recover owned orphan processes when PID records
are missing.

The default bindings are:

- `127.0.0.1:8000` for the FastAPI server;
- `0.0.0.0:3000` for the browser client.

Port 8000 should remain private. Route public traffic through the web service on
port 3000 or, preferably, through an HTTPS reverse proxy.

## First-run setup

Open the `/setup` URL printed by `start.sh` and enter the one-time bootstrap
code. The setup wizard then guides you through:

1. runtime checks;
2. creation of the administrator account;
3. password security;
4. optional local TOTP enrollment with a server-generated QR code;
5. TMDB metadata access;
6. server URL and port settings;
7. local or Google Drive storage;
8. final review and initialization.

StreamHome does not use SMTP or email OTP. Two-factor authentication is local
TOTP, and enrollment secrets remain server-owned.

Keep these values private:

- administrator passwords;
- TOTP secrets and recovery codes;
- MediaSender ingestion tokens;
- TMDB credentials;
- Google OAuth credentials and refresh tokens;
- Rclone configuration;
- session cookies and bootstrap codes.

## Important paths

Paths are relative to the StreamHome installation directory:

| Path | Purpose |
| --- | --- |
| `.env` | Linux process and deployment settings |
| `server/.env` | Server secrets and private configuration |
| `server/database.db` | Canonical SQLite database |
| `server/media` | Physical media catalog mounted at `/media` |
| `server/rclone/rclone.conf` | Application-managed Rclone configuration |
| `server/temp` | Temporary processing and playback data |
| `server/backup` | Local database backups |
| `web/dist` | Production browser assets |
| `.run` | Runtime PID and lock records |

Never publish environment files, `database.db`, Rclone configuration, backup
databases, authentication secrets, or recovery codes.

## Verification

After setup, confirm:

- the web URL opens;
- `/api/health` reports a ready server through the web proxy;
- the administrator can sign in;
- the selected storage health check succeeds;
- FFmpeg and FFprobe are available;
- the database and media directories are writable by the StreamHome user;
- a stop followed immediately by a start succeeds;
- the internal API is not publicly exposed.

For repository-level release validation, run:

```bash
./test.sh
```

That command checks the shell contracts, server regressions, Python environment
and database, frontend tests, TypeScript compilation, and production build.

## Troubleshooting

### The installer reports missing commands

Install the named packages with the distribution’s package manager, or rerun
without `--skip-system-packages` when the current account can use root or
`sudo`.

### Python or Node.js is too old

Check:

```bash
python3 --version
node --version
npm --version
```

StreamHome requires Python 3.11+ and Node.js 18+.

### The setup page does not open

Check the application logs and listener ownership:

```bash
cd ~/StreamHome
tail -n 100 backend.log
tail -n 100 frontend.log
ss -ltnp | grep -E ':(3000|8000)[[:space:]]'
```

Confirm that the firewall permits the configured web port and that no unrelated
process owns either StreamHome port.

### The bootstrap code is rejected

Confirm that the code came from the current installation’s latest `./start.sh`
output and that the browser is connected to that same server. Do not put the
code in a URL, screenshot, shell history, or public log.

### An update refuses a dirty checkout

Inspect the tracked changes:

```bash
cd ~/StreamHome
git status --short
```

Preserve or reconcile those changes before rerunning the installer. The
installer intentionally refuses to overwrite them.

## Related documentation

- [Getting Started](getting-started.md)
- [Initial Setup](setup.md)
- [Security](security.md)
- [Google Drive Integration](google-drive.md)
- [Adding Media](adding-media.md)
- [MediaSender Integration](mediasender.md)
- [Backup and Recovery](backup-and-recovery.md)
- [Troubleshooting](troubleshooting.md)
