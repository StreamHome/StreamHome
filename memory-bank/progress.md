# Progress Status

## Server

- [x] FastAPI, SQLModel/SQLite, FFmpeg, TMDB enrichment, download queue processing, local/cloud storage settings, recovery scanning, and range streaming are implemented.
- [x] The database path is standardized to the absolute `server/database.db` path resolved from `server/config.py`.
- [x] Physical media, artwork, subtitles, and `.metadata/metadata.json` records live under `server/media`; FastAPI exposes that catalog through `/media` and authenticated API routes.
- [x] Movie ingestion excludes `season` and `episode`; series ingestion supplies them when applicable.
- [x] Authentication uses password plus optional local TOTP. SMTP and email OTP support are removed.
- [x] Download progress is available through the authenticated SSE queue endpoint.
- [x] Storage and background HEVC settings are available through the server settings API.

The server was deliberately not changed during the current web repair. Existing detailed server behavior remains documented in the server code and `memory-bank/mediaSenderAPI.md`.

## Web repair completed

- [x] Replaced optimistic/mismatched client types with normalization at the API boundary for auth, movies, episodes, playback, profiles, settings, and queue events.
- [x] Fixed auth hydration, TOTP response-key normalization, profile restoration, and legacy `netflix` theme migration to `cinema`.
- [x] Removed all bundled poster/backdrop assets and mock media fallbacks. The web client now displays only server-provided media URLs; absent or unusable artwork becomes a neutral CSS placeholder.
- [x] Replaced the generic dashboard shell with shared server-data controllers and four distinct presentation systems: Obsidian Frost Ember, editorial Aurora, cinematic Cinema, and workspace Gemini.
- [x] Added canonical query navigation for `profile`, `view`, `media`, `genre`, `season`, `q`, and admin `section`, including validation, legacy redirects, refresh restoration, and browser history.
- [x] Implemented working catalog tabs, search, watchlist, details, TV season/episode selection, profile switching, and read-only download progress.
- [x] Rebuilt the player around server catalog records, authenticated stream URLs, quality selection, playback reporting, subtitles, and skip markers. Records without playable media are visibly unavailable.
- [x] Reduced the web admin center to supported server capabilities: Account/TOTP, Storage/HEVC, and read-only Downloads. Removed unsupported backup, update, user mutation, source URL, and queue-cancel controls.
- [x] Added an application error boundary and focused Vitest coverage for API normalization, playback payloads, store migration/hydration, and media URL filtering.

## Validation

- [x] `npm run lint`
- [x] `npm test`
- [x] `npm run build`
- [x] Browser validation: unchanged login, profile gallery, four themes at desktop/mobile/tablet widths, query navigation, search, details history, guards, admin sections, and a clean final console pass.
- [ ] `server/scratch/check_db.py` requires the server Python environment and is re-run as part of each final validation pass.

## Remaining server backlog

- [ ] Expose rclone upload progress in a supported server API before adding it to the web UI.
- [ ] Add server-side subtitle conversion only if it remains a product requirement.
