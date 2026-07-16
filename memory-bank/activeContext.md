# Active Context

## Current focus

The current web-only rebuild expands the repaired Ember experience into a system-wide discovery contract. Ember, Aurora, Cinema, and Gemini now receive server-backed Movies/Series billboards, genre rails, My List navigation, and in-application profile settings while retaining their own presentation systems. The server remains unchanged and is the source of truth for all media information.

## Current web architecture

- React routes are protected by hydrated auth and a query-aware profile guard that resolves `profile` against server profiles.
- API modules normalize the server's wire format into stable TypeScript models.
- The selected profile controls one of four canonical themes: Ember, Aurora, Cinema, or Gemini. Legacy, missing, and unknown values fall back to Ember; only explicit `cinema` selects Cinema.
- The authenticated application uses canonical query state such as `/?profile=1&view=series`; My List uses `view=watchlist`, and search, genres, details, seasons, playback, downloads, and admin sections remain deep-linkable.
- Shared catalog controller hooks own API behavior while a typed theme registry selects distinct Ember, Aurora, Cinema, and Gemini navigation, heroes, cards, details, and player presentation.
- Ember follows the Obsidian Frost reference with slow crossfades, artwork drift, sharp glass, orange edge glow, scanlines, serif display type, and technical mono labels. Aurora, Cinema, and Gemini retain separate editorial, cinematic, and workspace billboard identities.
- Movies and Series use rotating server-catalog billboards followed by per-genre horizontal rails. Rail controls are attached to the left/right edges and native horizontal scrollbars are hidden.
- The active-profile control opens Edit Profile, Switch Profile, and Sign Out actions. Editing can change the name or canonical theme without leaving the application; switching profiles remains explicit.
- `MediaArtwork` accepts direct server media paths and absolute HTTP(S) URLs. Compact server artwork filenames are resolved against the server-returned movie or episode identity into `/media/...` candidates; it never substitutes a bundled media image.
- The player resolves its movie or episode from authenticated server APIs and refuses playback when no physical media URL exists.
- Admin exposes only implemented server capabilities: current-account TOTP, storage/HEVC settings, and read-only download events.

## Security and data boundaries

- No media metadata or media files are stored in the web source tree.
- The ingestion API token is not exposed by the web client.
- SMTP/email OTP is not referenced; authentication and admin reauthentication use local TOTP.
- Queue source URLs are neither typed for UI use nor rendered.

## Next step

Frontend lint, 26 tests, the production build, live four-theme browser QA, media-boundary scans, diff checks, and the required web commit are complete. The server checker was attempted with both installed Python interpreters but cannot start because `sqlmodel` is not installed; no server files changed. No implementation follow-up is pending.
