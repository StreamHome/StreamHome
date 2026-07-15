# System Patterns

## System architecture

- **API layer:** FastAPI provides asynchronous auth, catalog, profile, playback, queue, stream, settings, and ingestion endpoints.
- **Persistence:** SQLModel uses the absolute SQLite database at `server/database.db`, resolved from `server/config.py`.
- **Media ownership:** All physical media, artwork, subtitles, and recovery metadata belong under `server/media`. FastAPI mounts media at `/media` for range-capable delivery.
- **Background work:** The singleton queue manager coordinates ingestion, FFmpeg processing, catalog updates, and optional cloud movement.
- **Recovery:** Server-side `.metadata/metadata.json` records allow disk-to-database catalog recovery.
- **External enrichment:** The server, not the web client, owns TMDB enrichment and local artwork caching.

## Web patterns

1. **Server-authoritative media**
   - The web client never ships media assets or invents media metadata.
   - Artwork components accept `/media/...` or absolute HTTP(S) URLs and otherwise render a CSS placeholder.

2. **Normalize at the API boundary**
   - Server snake_case and mixed wire keys are converted once in `web/src/api`.
   - Components consume stable camelCase TypeScript models.

3. **Hydrated query-state guarding**
   - `AuthGuard` waits for persisted auth hydration before redirecting.
   - `QueryProfileGuard` resolves the `profile` query parameter against server profiles; the URL overrides stale local state.
   - The root application URL carries validated `view`, `media`, `genre`, `season`, `q`, and `section` state. Unsupported parameters are discarded during canonicalization.

4. **Shared behavior, themed presentation**
   - Shared controller hooks own catalog/search/watchlist/playback API behavior.
   - A typed registry selects distinct navigation, hero, card, details, and player variants for Ember, Aurora, Cinema, and Gemini.
   - Theme styles are scoped to authenticated application roots so the login screen remains unchanged.

5. **Playable-state honesty**
   - Catalog records may exist before physical media is available.
   - Details and player routes visibly disable or reject playback when the server provides no usable media URL.

6. **Supported admin surface only**
   - Web admin controls map to real server endpoints.
   - Unsupported backup/update/user mutation and download cancellation UI is omitted.
   - Sensitive settings changes require password and optional TOTP reauthentication.

7. **Authenticated streaming and progress**
   - Stream URLs include the current auth token and selected playback options.
   - Playback progress is periodically reported using the server's expected payload keys.
   - Download progress is consumed as a read-only authenticated SSE stream.

## Server invariants

- Movie ingestion payloads omit `season` and `episode` entirely.
- SMTP and email OTP libraries/configuration must not return.
- Media metadata files store quality, languages, and subtitles alongside server media.
- `/media` is excluded from large client-side caching strategies.
