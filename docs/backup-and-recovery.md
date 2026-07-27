# Backup and Recovery

The primary catalog database is `server/database.db`. Media portability metadata is stored in `.metadata/metadata.json` beside ingested movies and episodes.

## Before relying on a backup

- Create a backup through the Admin interface.
- Confirm the backup file exists and has a current timestamp.
- Keep at least one copy outside the StreamHome server.
- Test restoration on a disposable installation.
- Preserve the server `.env`, root `.env`, and encrypted Rclone configuration separately.

## Restoration

Stop StreamHome before restoring a database:

```bash
./stop.sh
```

Use the authenticated Admin restore workflow when the application is operational. Manual file replacement should be reserved for disaster recovery and must preserve ownership and permissions.

After restoration:

```bash
cd server
PYTHONPATH=. ../venv/bin/python scratch/check_db.py
cd ..
./start.sh
```

If the database cannot be recovered, the catalog recovery scanner can rebuild supported media records from `.metadata/metadata.json`. Account credentials, sessions, and playback history still require a database backup.
