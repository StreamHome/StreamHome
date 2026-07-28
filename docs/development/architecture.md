# Architecture

StreamHome consists of:

- a FastAPI backend in `server/`;
- a SQLite catalog fixed at `server/database.db`;
- FFmpeg/FFprobe media processing;
- an encrypted application-owned Rclone configuration for optional Google Drive storage;
- a React, TypeScript, and Vite web client in `web/`;
- Linux lifecycle scripts at the repository root.

The web server listens on the configured public port and proxies `/api` and `/media` to FastAPI bound explicitly to `127.0.0.1:8000`. Physical media lives under `server/media`.

Linux lifecycle operations are serialized by installation, setup, and start/stop locks. Bootstrap promotes only a complete temporary checkout, setup stops an owned runtime before replacing dependencies or assets, PID records are written atomically, startup reports success only after API and web health checks pass, and shutdown preserves evidence when an owned process cannot be terminated.

Web-managed updates add a separate owner-recorded update lock and detached controller. Candidate code is dependency-installed and production-built in an isolated sibling checkout while the current release remains online. Idle and automatic requests receive a second fail-closed idle check before cutover. Explicit immediate requests bypass viewer, playback, maintenance-window, and idle-grace delays, but the backend still refuses cutover during active transfers, media processing, backup/restore work, or unrelated API mutations. During stopped-runtime work, a minimal maintenance responder occupies the public web port. The controller checkpoints SQLite, applies only the exact validated fast-forward, uses the normal setup/start lifecycle, and records success only after both API and web health gates pass. Failed cutovers restore the old commit and database checkpoint before rebuilding and health-checking the previous release.

Ingestion is queue-based. A single FFmpeg input graph can fan out to a lossless final MP4 and a growing, application-owned 720p H.264/AAC fMP4 HLS preview under `server/temp/ingest_preview`. Preview readiness uses playlist duration plus observed FFmpeg speed to require a safe adaptive lead. Playback runs snapshot either the catalog fingerprint or an opaque ingestion-task fingerprint, allowing an active preview run to remain valid when catalog finalization replaces the temporary source with local or cloud media. Finalization writes portable `.metadata/metadata.json` beside media so catalog recovery can reconstruct quality, language, and subtitle information.

Authentication uses HttpOnly sessions and optional local TOTP. TOTP enrollment state is server-owned, encrypted at rest, short-lived, and bound to the initiating setup or authenticated user session. The server renders the enrollment QR as a non-cacheable SVG and promotes the pending secret to the user only after successful verification. SMTP and email OTP are intentionally absent.

Profile-owned history, resume state, watchlists, recommendation candidates, tastes, preferences, telemetry, exposures, and playback runs are durable SQLite records. The recently reauthenticated admin profile-data API reads those records without changing the selected playback profile or bypassing normal profile access for non-admin routes. TMDB `Movie` rows and their artwork/portable metadata are intentionally shared server cache resources; the admin explorer reports profile associations without assigning file ownership to a profile. Recommendation shadow-comparison metrics are persisted rather than held only in process memory, and browser exposure batches remain in a bounded retry queue until the server acknowledges them.

Profile deletion explicitly removes all profile-owned rows and attempt milestones in one transaction. Shared catalog rows, physical media, artwork, and TMDB cache files remain intact.

Release changes must preserve the fixed database/media paths, the application-owned Rclone command path, and the movie-ingestion payload rule.
