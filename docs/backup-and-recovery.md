# Backup and Recovery

The primary catalog database is `server/database.db`. Media portability metadata is stored in `.metadata/metadata.json` beside ingested movies and episodes.

## Before relying on a backup

- Create a backup through the Admin interface.
- Confirm the backup file exists and has a current timestamp.
- Keep at least one copy outside the StreamHome server.
- Test restoration on a disposable installation.
- Preserve the server `.env`, root `.env`, and encrypted Rclone configuration separately.

## Restoration

The authenticated Admin restore workflow first refuses active playback/downloads, quiesces other requests, validates the backup, creates a rollback backup, and atomically installs the selected database. A successful restore deliberately leaves the API in maintenance mode. Restart StreamHome immediately before making any other request:

```bash
./stop.sh
./start.sh
```

For a manual disaster-recovery replacement, stop StreamHome before touching `server/database.db` and preserve file ownership and permissions.

After restoration:

```bash
cd server
PYTHONPATH=. ../venv/bin/python scratch/check_db.py
cd ..
./start.sh
```

If the database cannot be recovered, the catalog recovery scanner can rebuild supported local or configured-cloud media records from `.metadata/metadata.json`. Account credentials, sessions, and playback history still require a database backup.
