# Architecture

StreamHome consists of:

- a FastAPI backend in `server/`;
- a SQLite catalog fixed at `server/database.db`;
- FFmpeg/FFprobe media processing;
- an encrypted application-owned Rclone configuration for optional Google Drive storage;
- a React, TypeScript, and Vite web client in `web/`;
- Linux lifecycle scripts at the repository root.

The web server listens on the configured public port and proxies `/api` and `/media` to FastAPI on loopback port 8000. Physical media lives under `server/media`.

Ingestion is queue-based. Finalization writes portable `.metadata/metadata.json` beside media so catalog recovery can reconstruct quality, language, and subtitle information.

Authentication uses HttpOnly sessions and optional local TOTP. TOTP enrollment state is server-owned, encrypted at rest, short-lived, and bound to the initiating setup or authenticated user session. The server renders the enrollment QR as a non-cacheable SVG and promotes the pending secret to the user only after successful verification. SMTP and email OTP are intentionally absent.

Release changes must preserve the fixed database/media paths, the application-owned Rclone command path, and the movie-ingestion payload rule.
