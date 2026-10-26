# Troubleshooting

## Maintenance page does not finish after an update

A maintenance response can be expected during protected cutover. An unresolved
`recovery_required` operation needs explicit review; do not delete its checkpoint,
runtime generations, journal, database or media to clear the page.

For a managed installation:

```bash
sudo streamhome status
sudo streamhome doctor
sudo streamhome logs --lines 120
sudo streamhome recover --operation OPERATION_UUID --review
```

Only actions supported by the exact current evidence are offered. Continuation,
repair and service resumption have separate permission boundaries. Repeating the
installer does not install a different recovery controller or clear an operation.
See [Installation](installation.md) and [Backup and Recovery](backup-and-recovery.md).

Legacy checkout installations keep their release's supported inspection/start
scripts. Their `.run` and source-relative database paths differ from managed
paths; do not apply commands for a different layout.

## StreamHome does not start

For managed installations, inspect `sudo streamhome doctor` and
`sudo streamhome logs --lines 120`, then use `sudo streamhome start` when the
reported state permits it. Check for unrelated owners of the configured web port
and internal port 8000. A listener conflict does not authorize terminating another
service. Use `sudo streamhome configure` to review a different web port.

For a legacy checkout, run its supported scripts from its actual installation
directory and inspect that release's backend/frontend logs. Do not delete a lock
owned by a live process or force-start around unresolved lifecycle authority.

## Setup returns to the unlock screen

The signed setup session expired. Enter the current bootstrap code again. A still-active Google Drive setup job will be rebound from the saved browser checkpoint.

## TMDB validation expired

Return to the TMDB step and validate the token again. The validation receipt is deliberately bound to the current setup session and token.

## Google Drive fails

Managed installations use their manifest-bound application Rclone runtime. Start
with `sudo streamhome doctor` and the storage health checks, then consult
[Google Drive Storage](google-drive.md). Do not replace application-managed
credentials using a global `rclone config`.

## Database errors

The managed catalog is `/var/lib/streamhome/database.db`; legacy checkouts retain
`server/database.db`. Inspect the installation's lifecycle and backup evidence
before modifying either. `server/scratch/check_db.py` can initialize or change a
database and is not a read-only production diagnostic. Test recovery procedures
against a disposable copy before an authorized valuable-data operation.

## Release checks refuse to start

`test.sh` deliberately refuses to run while the API or web port is active. Run `./stop.sh` first. Use `./test.sh --server-only`, `./test.sh --web-only`, or `./test.sh --syntax-only` when a narrower diagnostic is appropriate.

## Browser shows old assets

Perform a normal hard refresh. The alpha does not register a service worker, so there should be no StreamHome offline cache to remove.
