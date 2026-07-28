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
4. install the exact preflighted commit;
5. reinstall locked dependencies and rebuild production assets;
6. start StreamHome through `start.sh`;
7. require both the database-backed API health check and production web response to pass.

An update is successful only after both services are healthy. If installation, build, API startup, or web startup fails, the controller restores the previous commit and database checkpoint, rebuilds that known-working release, restarts it, and verifies it again. The failed target is suppressed from automatic retry until an administrator explicitly selects **Retry failed target**.

Lifecycle output is stored in `update.log`. While cutover is active, Cloudflare or another reverse proxy receives a maintenance response rather than an unresponsive origin whenever the temporary responder can bind the configured port.

## Command-line fallback

If the admin panel is unavailable, inspect `update.log` and `.run/update-state.json`. A reviewed manual update remains available:

```bash
cd ~/StreamHome
./stop.sh
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The installer verifies the official origin, refuses a dirty checkout, and permits only an exact fetched fast-forward. Do not delete `server/database.db`, `server/media`, `.env`, `server/.env`, `server/backup`, or `server/rclone`.
