# Active Context

## Current focus

The current work is a web-only reliability repair. The server is the source of truth for the media catalog, artwork, metadata, streaming URLs, episodes, subtitles, skip markers, queue progress, and system settings.

## Current web architecture

- React routes are protected by hydrated auth and profile guards.
- API modules normalize the server's wire format into stable TypeScript models.
- The selected profile controls one of four themes: Ember, Aurora, Cinema, or Gemini. Legacy `netflix` profile values migrate to `cinema`.
- A shared `DashboardShell` provides catalog, search, watchlist, profiles, details, and downloads behavior for every theme.
- `MediaArtwork` accepts only server media paths or absolute HTTP(S) URLs. It never substitutes a bundled media image.
- The player resolves its movie or episode from authenticated server APIs and refuses playback when no physical media URL exists.
- Admin exposes only implemented server capabilities: current-account TOTP, storage/HEVC settings, and read-only download events.

## Security and data boundaries

- No media metadata or media files are stored in the web source tree.
- The ingestion API token is not exposed by the web client.
- SMTP/email OTP is not referenced; authentication and admin reauthentication use local TOTP.
- Queue source URLs are neither typed for UI use nor rendered.

## Next step

Complete the required validation and commit the web repair. Future web features must first be backed by an existing server endpoint or a separately approved server change.
