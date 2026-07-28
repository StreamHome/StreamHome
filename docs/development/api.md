# API Development Notes

The FastAPI application is served on loopback port 8000 and proxied by the web server under the same browser origin.

Important route families:

- `/api/setup/*`: first-run setup and Google Drive connection;
- `/api/auth/*`: cookie authentication, TOTP, sessions, and integration credentials;
- `/api/profiles/*`: profile management and PIN verification;
- `/api/add-movie`: scoped ingestion;
- `/api/playback/*` and `/api/stream/*`: playback preparation and delivery;
- `/api/system/*`: authenticated administration;
- `/media/*`: range-capable physical media delivery.

API-key management is available only to an authenticated administrator with recent reauthentication:

- `GET /api/auth/integrations`: list keys without revealing their secrets;
- `GET /api/auth/integrations/scopes`: list assignable permissions;
- `POST /api/auth/integrations`: create a named key and return its secret once;
- `PUT /api/auth/integrations/{credential_id}`: update its name and permissions;
- `DELETE /api/auth/integrations/{credential_id}`: revoke one key.

Supported machine permissions are:

- `ingest`: submit media through `POST /api/add-movie`;
- `downloads:read`: read `GET /api/downloads` or its `/stream` SSE variant;
- `downloads:cancel`: cancel and remove a task through `DELETE /api/downloads/{task_id}`.

API keys never authorize account security, backups, server settings, profile PIN operations, or playback.

Update lifecycle endpoints require a recently reauthenticated administrator, except for the authenticated browser-presence heartbeat and the loopback-only ephemeral controller handoff:

- `GET /api/update/status`: current/target commits, lifecycle state, idle blockers, policy, and bounded log tail;
- `POST /api/update/check`: fetch and validate the official update channel;
- `PUT /api/update/policy`: configure automatic checks, idle grace, and the optional maintenance window;
- `POST /api/update/install`: queue the validated target for installation after verified idle;
- `DELETE /api/update/pending`: cancel work that has not entered preflight;
- `POST /api/update/presence`: record or clear visible authenticated browser presence.

Machine API keys cannot use update endpoints.

Use the generated FastAPI OpenAPI document from a development server for the exact schema. Security-sensitive mutations must use the existing authentication, same-origin, rate-limit, and recent-reauthentication dependencies.

TOTP setup begins with a server-owned enrollment. The client receives an opaque enrollment identifier, a one-time manual key, and a same-origin SVG QR URL. Verification sends the enrollment identifier and current six-digit code; it never posts the TOTP secret back to the server. Setup enrollments are bound to the setup-session hash, while administrator enrollments are bound to both the user and current authenticated session.

Movie ingestion JSON must omit `season` and `episode`; sending either key with `null` is not equivalent.
