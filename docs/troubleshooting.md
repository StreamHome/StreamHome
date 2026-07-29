# Troubleshooting

## Maintenance page does not finish after an update

The maintenance page is normal only during the short protected activation and health-check window. Current releases display the live phase, transaction identifier, elapsed time, rollback state, and any safe recovery code. They automatically detect a lost update controller and queue recovery once.

From the StreamHome installation directory, inspect the durable state without deleting application data:

```bash
cat .run/update-state.json
tail -n 120 update.log
cat .run/update-diagnostics.json 2>/dev/null || true
./start.sh
```

`./start.sh` does not compete with a live updater. It waits for verified controller ownership; if that ownership is stale, it restores the recorded previous release and starts both services through the normal health gates. Do not delete `.run`, the staging checkout, `server/database.db`, `server/media`, `web/dist`, or `web/node_modules` while recovery is active, because they may contain the verified checkpoint and rollback artifacts.

For an installation still running the legacy updater, use the current installer without deleting the StreamHome directory:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

This retrieves the current recovery-capable controller before cutover.

## StreamHome does not start

Run:

```bash
cd ~/StreamHome
./stop.sh
./start.sh
```

Inspect `backend.log` and `frontend.log`. Confirm ports 3000 and 8000 are not owned by unrelated services.

`start.sh` waits up to 30 seconds for the database-backed API health endpoint and production web response. If either service fails, it prints both log tails, stops the partial process tree, and returns a nonzero status instead of reporting a false success.

If a lifecycle-lock error appears, first confirm that no other `start.sh` or `stop.sh` command is running. Dead-owner locks are recovered automatically; do not remove a lock owned by a live process.

## Setup returns to the unlock screen

The signed setup session expired. Enter the current bootstrap code again. A still-active Google Drive setup job will be rebound from the saved browser checkpoint.

## TMDB validation expired

Return to the TMDB step and validate the token again. The validation receipt is deliberately bound to the current setup session and token.

## Google Drive fails

Run `./setup.sh` again to verify Rclone 1.68 or newer, then consult [Google Drive Storage](google-drive.md). Do not run `rclone config`; StreamHome uses its own encrypted configuration.

When the system Rclone is missing or too old, setup installs the pinned application-owned build from a versioned official archive and verifies its embedded release checksum before atomic activation.

## Database errors

Run from `server/`:

```bash
PYTHONPATH=. python scratch/check_db.py
```

The only supported catalog database is `server/database.db`.

## Release checks refuse to start

`test.sh` deliberately refuses to run while the API or web port is active. Run `./stop.sh` first. Use `./test.sh --server-only`, `./test.sh --web-only`, or `./test.sh --syntax-only` when a narrower diagnostic is appropriate.

## Browser shows old assets

Perform a normal hard refresh. The alpha does not register a service worker, so there should be no StreamHome offline cache to remove.
