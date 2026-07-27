# Troubleshooting

## StreamHome does not start

Run:

```bash
cd ~/StreamHome
./stop.sh
./start.sh
```

Inspect `backend.log` and `frontend.log`. Confirm ports 3000 and 8000 are not owned by unrelated services.

## Setup returns to the unlock screen

The signed setup session expired. Enter the current bootstrap code again. A still-active Google Drive setup job will be rebound from the saved browser checkpoint.

## TMDB validation expired

Return to the TMDB step and validate the token again. The validation receipt is deliberately bound to the current setup session and token.

## Google Drive fails

Run `./setup.sh` again to verify Rclone 1.68 or newer, then consult [Google Drive Storage](google-drive.md). Do not run `rclone config`; StreamHome uses its own encrypted configuration.

## Database errors

Run from `server/`:

```bash
PYTHONPATH=. python scratch/check_db.py
```

The only supported catalog database is `server/database.db`.

## Browser shows old assets

Perform a normal hard refresh. The alpha does not register a service worker, so there should be no StreamHome offline cache to remove.
