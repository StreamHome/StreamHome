# Initial Setup

Run `./start.sh` from the installation directory. The command prints:

- the browser-facing setup URL;
- a one-time bootstrap code;
- the log locations.

The setup wizard verifies the runtime, creates the administrator account, optionally enables local TOTP, validates TMDB access, configures the public URL and web port, and selects local or Google Drive storage.

## Important behavior

- Setup credentials and passwords are not stored in browser checkpoints.
- A setup session lasts two hours.
- An active Google Drive job can be rebound after the bootstrap code is entered again.
- TMDB validation produces a signed setup receipt; completion does not depend on a second TMDB network request.
- Configuration files are staged before database changes are committed. A failed completion restores the previous configuration.

## Local storage

Local media is stored under the absolute `server/media` directory. The SQLite catalog remains at `server/database.db`.

## Google Drive

Follow [Google Drive Storage](google-drive.md). Rclone is controlled only by StreamHome through its encrypted application-owned configuration.

## Completion

Save the one-time MediaSender ingestion token and any TOTP recovery codes before leaving the completion page. Restart completion is handled by `start.sh` on the supported Linux server path.
