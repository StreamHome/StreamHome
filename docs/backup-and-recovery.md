# Backup and Recovery

The primary catalog database is `server/database.db`. Media portability metadata is stored in `.metadata/metadata.json` beside ingested movies and episodes.

## Before relying on a backup

- Create a backup through the authenticated Admin interface.
- Confirm the backup is listed as valid and has a current timestamp.
- Keep at least one copy outside the StreamHome server.
- Test restoration on a disposable installation.
- Preserve the server `.env`, root `.env`, and encrypted Rclone configuration separately.

New backups use an unambiguous `backup_v1_*.db` name and a matching private
`.manifest.json` file. The versioned manifest records the database filename,
size, SHA-256 content identity, creation time, database contract version, and
application build provenance. The database and manifest are one logical backup:
copy, retain, and delete them together. The hash detects mismatch; it is not a
signature and does not prove authenticity against someone able to replace both
files.

Older standalone `backup_*.db` files remain supported after the existing
containment, SQLite integrity, and schema checks. A new-format backup without its
manifest fails closed. A corrupt, unsupported, or mismatching manifest is never
ignored, including when one is present beside a legacy-named backup. SQLite
backup files with sibling `-wal`, `-shm`, or `-journal` state are not accepted as
standalone restore candidates.

## Restoration

Use the authenticated Admin restore workflow. Recent administrator
reauthentication is required. StreamHome validates the contained backup before
claiming restore authority, rejects new work, drains bounded in-flight work,
stops the managed runtime, preserves a rollback checkpoint, activates the
candidate atomically, and starts it in guarded verification mode. A successful
restore completes mandatory recovery and restarts normal service automatically;
a manual restart is not the normal success path.

Before the restore acceptance boundary, failed verification can automatically
restore the protected previous database. After acceptance, automatic rollback is
disabled and recovery continues fail closed. Never replace `server/database.db`
while StreamHome is running and do not use the legacy `server/cli.py` menu for
restore.

If maintenance reports `recovery_required`, automatic destructive retry has
stopped. Preserve `.run/lifecycle`, update/restore journals, staged candidates,
and rollback checkpoints. First run the local read-only planner:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server \
  ./venv/bin/python -m lifecycle.recovery --root "$PWD" plan \
  --operation-id <exact-operation-id> --generation <exact-generation>
```

The planner inspects exact retained evidence and reports only evidence-supported
action identifiers plus an `evidenceIdentity`. It does not take ownership, clear
lifecycle state, delete artifacts, resume, or roll back an operation. Review the
result, then explicitly execute one action that the current plan permits:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server \
  ./venv/bin/python -m lifecycle.recovery --root "$PWD" execute \
  --operation-id <exact-operation-id> --generation <exact-generation> \
  --action <permitted-action> --evidence-identity <exact-evidence-identity>
```

Execution rechecks the evidence, acquires exclusive controller ownership, rotates
the lifecycle generation and private capability, and delegates to the existing
guarded restore or update controller. There is no force or force-clear option.
Restore rollback is never offered after acceptance. Update rollback is not offered
because current update checkpoints do not contain sufficient transaction-bound
identity evidence. A failed or interrupted operator action remains fail closed;
run `plan` again rather than assuming the old action is still valid.

Successful execution attempts exact resolved-artifact cleanup while retaining its
compact audit record. If it reports `operator_cleanup_pending`, retry cleanup only
for the reported controller generation:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server \
  ./venv/bin/python -m lifecycle.recovery --root "$PWD" cleanup \
  --operation-id <exact-operation-id> --generation <controller-generation>
```

Cleanup cannot clear an active or `recovery_required` operation. Do not manually
delete recovery artifacts to make startup proceed, and do not expect `start.sh` to
execute a persisted operator action automatically.

If the database cannot be recovered, the catalog recovery scanner can rebuild
supported local or configured-cloud media records from `.metadata/metadata.json`.
Account credentials, sessions, and playback history still require a database
backup.
