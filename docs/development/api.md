# API Development Notes

The FastAPI application is served on loopback port 8000 and proxied by the web server under the same browser origin.

Important route families:

- `/api/setup/*`: first-run setup and Google Drive connection;
- `/api/auth/*`: cookie authentication, TOTP, sessions, and integration credentials;
- `/api/profiles/*`: profile management and PIN verification;
- `/api/admin/profiles/*`: recently reauthenticated cross-profile data inspection;
- `/api/add-movie`: scoped ingestion;
- `/api/playback/*`: authenticated playback runs, preparation, and ticket-protected delivery;
- `/api/system/*`: authenticated administration;
- `/media/*`: allowlisted safe presentation assets; protected media uses playback routes.

API-key management is available only to an authenticated administrator with recent reauthentication:

The web manager is a server-wide **Admin → API Keys** panel. It is not associated with the profile selected for profile-data inspection.

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

Unsafe extension requests to integration-capable routes may include both an explicit `shk_` Bearer key and an existing browser session cookie. The security boundary treats those requests as machine authentication only on the allowlisted ingestion/download routes; the route must validate the key and may not fall back to cookie authorization when the key is invalid. Authentication failures use stable `missing_integration_credential`, `invalid_integration_credential`, and `insufficient_scope` codes.

Update lifecycle endpoints require a recently reauthenticated administrator, except for the authenticated browser-presence heartbeat and the loopback-only ephemeral controller handoff:

- `GET /api/update/status`: current/target commits, lifecycle state, idle blockers, policy, and bounded log tail;
- `POST /api/update/check`: fetch and validate the official update channel;
- `PUT /api/update/policy`: configure automatic checks, idle grace, and the optional maintenance window;
- `POST /api/update/install`: request `mode: "when_idle"` or `mode: "now"`; both paths preflight and health-gate the exact target, while immediate mode bypasses only viewer/playback/idle delays and retains protected-write blockers;
- `DELETE /api/update/pending`: cancel work that has not entered preflight;
- `POST /api/update/presence`: record or clear visible authenticated browser presence.

Machine API keys cannot use update endpoints.

`POST /api/add-movie` accepts optional `video_source_type` and `audio_source_type` values of `"auto"` or `"hls"`. MediaSender clients should send `"hls"` when they identify a manifest behind a misleading filename such as `master.txt`. The server persists the resolved source type with the queued task and applies the supplied allowlisted headers to both probing and FFmpeg ingestion.

Processing catalog responses may include an opaque `previewTaskId`; they never serialize the submitted source URL while that preview is active. Playback-run creation automatically selects the preview source for the matching task. Ready previews use ticket-protected `GET /api/playback/preview/{media_id}/playlist.m3u8` and `/api/playback/preview/{media_id}/{path}` routes. These routes accept only a ticket for the exact authenticated session, profile, playback run, media ID, and immutable preview fingerprint. They rewrite every playlist URI back through StreamHome and never expose ingestion headers.

Administrative profile-data endpoints require recent reauthentication and do not require selecting or unlocking the inspected profile:

- `GET /api/admin/profiles`: summary counts for every profile and the storage-location map;
- `GET /api/admin/profiles/{profile_id}/overview`: aggregate counts, watch time, activity, and persistence classifications;
- `GET /api/admin/profiles/{profile_id}/history`: paginated viewing attempts plus current resume states;
- `GET /api/admin/profiles/{profile_id}/watchlist`: paginated saved titles in server order;
- `GET /api/admin/profiles/{profile_id}/recommendations`: persisted candidates, reasons, tastes, preferences, refresh state, and shadow metrics;
- `GET /api/admin/profiles/{profile_id}/activity`: accepted telemetry, exposures, and playback runs;
- `GET /api/admin/profiles/{profile_id}/cache`: shared TMDB cache records associated with the profile and their on-disk asset state.

These endpoints never serialize profile PIN hashes, authentication secrets, ingestion source headers, or absolute filesystem paths.

Use the generated FastAPI OpenAPI document from a development server for the exact schema. Security-sensitive mutations must use the existing authentication, same-origin, rate-limit, and recent-reauthentication dependencies.

TOTP setup begins with a server-owned enrollment. The client receives an opaque enrollment identifier, a one-time manual key, and a same-origin SVG QR URL. Verification sends the enrollment identifier and current six-digit code; it never posts the TOTP secret back to the server. Setup enrollments are bound to the setup-session hash, while administrator enrollments are bound to both the user and current authenticated session.

Movie ingestion JSON must omit `season` and `episode`; sending either key with `null` is not equivalent.

### Browser playback delivery

Playback-run responses include an optional `streamBaseUrl`. Browser clients use its
same-origin `manifest`, `progressive`, `source/{trackId}`, `subtitles/{trackId}`,
`hls/{path}` and `preview/{path}` assets with the current human session cookie.
Each request revalidates the session/profile/run/source; the run ID is not a bearer
credential. Ticket URLs remain supported and retain their expiry/revocation rules.
`rendition_id` on a master selects one verified video variant without dropping its
alternate audio group. In hybrid storage, viewer entry/status may enqueue bounded,
deduplicated preparation of a missing cache for the same accepted source generation.
Changed source media cannot be accepted through this path. Preparation runs in
owned background jobs with capacity admission.

Progress and Close optionally accept `total_seconds_played` for cumulative,
idempotent watch-time snapshots. Legacy `duration_watched` remains accepted.
Close additionally accepts `discard_progress: true` to abandon cancelled work
without modifying saved profile progress. Default Close still records the final
confirmed position and watch time.

### Storage administration

All `/api/admin/storage` endpoints require a recently reauthenticated human
session; integration credentials and browser query state grant no authority.

| Method and suffix | Purpose |
| --- | --- |
| `GET /status` | Cached policy, capacity, connection and current operation |
| `POST /refresh` | Enqueue a measurement with an exact `request_id` |
| `POST /policy/preview` | Bind the canonical `draft` and `expected_revision` to a review |
| `PUT /policy` | Accept that exact `review_id`, draft, revision and request identity |
| `GET /operations/{request_id}` | Reconcile the original request; response contains `operation` |
| `GET /retention` | Bounded local-retention inventory |
| `PUT /retention/{id}` | Set `keep_local` using the qualified item `id` |
| `GET /activity` | Bounded operation history |
| `POST /cleanup/preview` | Review currently eligible cleanup |
| `POST /cleanup` | Execute the exact `plan_id` with a request identity |

Retention item IDs distinguish `movie:` from `episode:`; do not substitute a
series parent's `media_id`. Pagination uses `cursor` and `limit`. Unknown byte
counts are null. Policy byte budgets are total integer allocations, not file-size
limits. A saved policy is not evidence of released space. A lost mutation response
must be reconciled with its original request identity before any retry; even HTTP
409 `storage_save_outcome_uncertain` means the outcome needs checking.

See [Storage Management](../storage-management.md) for accounting, placement,
offline readiness and recovery boundaries.
