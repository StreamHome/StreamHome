# Updating StreamHome

Administrators can manage updates from **Admin center → Updates**. The page reports the installed and available commits, the last check, current idle blockers, lifecycle progress, rollback state, and the latest update log.

## Manual updates

1. Open the Updates panel and select **Check now**.
2. Review the available commit and current activity blockers.
3. Choose an installation mode and reauthenticate:
   - **Update now** starts isolated preflight immediately and, after confirmation, bypasses browser presence, playback, the maintenance window, and the configured idle grace period. Viewers may be disconnected during the protected cutover.
   - **Install when idle** waits for the configured idle grace period and for playback, ingestion, downloads, media processing, backup/restore work, browser presence, and conflicting lifecycle work to stop.
4. For **Update now**, active ingestion, downloads, FFmpeg/media processing, backup/restore work, and unrelated API mutations must still finish or be cancelled. StreamHome reports these protected blockers instead of interrupting a potentially destructive write.

Both modes first build and validate the candidate in an isolated temporary checkout. A failed preflight does not stop or modify the running installation.

## Automatic updates

The Updates panel can enable automatic updates and configure:

- an idle grace period from 5 to 120 minutes;
- an update-check interval from 1 to 24 hours;
- an optional maintenance window in the server's local timezone.

Both maintenance times must be set or both must be empty. Overnight windows are supported. Leaving them empty permits installation at any verified-idle time.

The update channel is the official `StreamHome/StreamHome` `main` branch. Updates must be clean, exact fast-forwards from the installed commit. If signed-commit enforcement is enabled through server configuration, the candidate commit must also pass Git verification.

## Cutover and recovery

After preflight, the detached Linux update controller asks the running backend to reserve a protected cutover. Idle and automatic requests re-confirm full idle state. Immediate requests ignore viewers, playback, the maintenance window, and idle grace, but still refuse active transfers, media processing, backups/restores, or unrelated API mutations. Once approved, the controller:

1. stop the owned StreamHome processes;
2. create and integrity-check a recovery copy of `server/database.db`;
3. serve a temporary HTTP 503 maintenance page on the configured web port;
4. activate the exact preflighted commit, offline Python packages, prepared Node runtime, and production assets;
5. start StreamHome through `start.sh`;
6. require both the database-backed API health check and production web response to pass.

The expensive network fetches, dependency resolution, `npm ci`, and frontend build complete before shutdown. An update is successful only after both services are healthy. Startup allows a bounded cold-start window and retries a failed launch after cleaning up partial processes. If activation, API startup, or web startup fails, the controller restores the previous commit, database checkpoint, prepared web runtime, and offline Python packages, restarts that known-working release, and verifies it again. The failed target is suppressed from automatic retry until an administrator explicitly selects **Retry failed target**.

Lifecycle output is stored in `update.log`. While cutover is active, Cloudflare or another reverse proxy receives a maintenance response rather than an unresponsive origin whenever the temporary responder can bind the configured port.

Each cutover has a durable transaction identifier, controller lease, process-start identity, heartbeat, prepared-artifact location, and recovery state. The maintenance responder checks that lease independently. If the controller is terminated, killed by the kernel, loses its shell, or stops heartbeating, the responder changes from the active phase to recovery, queues the detached restart/recovery handoff once, and releases the public port. `start.sh` then restores the recorded previous commit, verified database checkpoint, Python wheelhouse, Node runtime, and production assets before applying the normal API and web health gates.

The maintenance response is never a cacheable static success claim. It reports the current safe lifecycle phase, elapsed time, transaction and target identifiers, rollback progress, and a diagnostic reference for terminal failures. Responses include browser, CDN, and surrogate no-store controls plus cache-busted polling. A stale or reused PID cannot retain update or maintenance ownership because PID records are bound to process start identity.

An interrupted update has only two accepted healthy terminal states: the target release passes both health gates, or the previous release is restored and passes both gates. Recovery artifacts and the database checkpoint are retained until one of those states is verified. They are not removed merely because the original controller exited.

## Command-line fallback

If the admin panel is unavailable, inspect `update.log`, `.run/update-state.json`, and the private `.run/update-diagnostics.json` snapshot. A reviewed manual update remains available:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

Do not stop StreamHome first. For an existing installation, the installer verifies the official origin, refuses a dirty checkout, preflights the fetched release while the current services remain online, and hands the exact fast-forward to the same health-gated cutover and rollback controller. The command does not return successfully until both services are healthy again. A concurrent `./start.sh` waits for the active updater and reports its phase instead of fighting the cutover.

If a host was left on the legacy indefinite maintenance responder by an older release, run the same installer command without deleting the installation. The fetched controller performs the new transaction-aware recovery. `./start.sh` is also safe: it waits for a live controller, rejects stale ownership, and recovers an interrupted transaction before ordinary startup.

Do not delete `server/database.db`, `server/media`, `.env`, `server/.env`, `server/backup`, or `server/rclone`.
