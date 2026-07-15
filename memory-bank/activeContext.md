# Active Context

## Current focus

The current work is a web-only reliability repair. The server is the source of truth for the media catalog, artwork, metadata, streaming URLs, episodes, subtitles, skip markers, queue progress, and system settings.

## Current web architecture

- React routes are protected by hydrated auth and a query-aware profile guard that resolves `profile` against server profiles.
- API modules normalize the server's wire format into stable TypeScript models.
- The selected profile controls one of four themes: Ember, Aurora, Cinema, or Gemini. Legacy `netflix` profile values migrate to `cinema`.
- The authenticated application uses canonical query state such as `/?profile=1&view=series`; search, genres, details, seasons, playback, downloads, and admin sections are deep-linkable.
- Shared catalog controller hooks own API behavior while a typed theme registry selects distinct Ember, Aurora, Cinema, and Gemini navigation, heroes, cards, details, and player presentation.
- Ember follows the Obsidian Frost reference: sharp obsidian glass, restrained orange glow, scanlines, serif display type, and technical mono labels. Aurora, Cinema, and Gemini retain separate editorial, cinematic, and workspace identities.
- `MediaArtwork` accepts only server media paths or absolute HTTP(S) URLs. It never substitutes a bundled media image.
- The player resolves its movie or episode from authenticated server APIs and refuses playback when no physical media URL exists.
- Admin exposes only implemented server capabilities: current-account TOTP, storage/HEVC settings, and read-only download events.

## Security and data boundaries

- No media metadata or media files are stored in the web source tree.
- The ingestion API token is not exposed by the web client.
- SMTP/email OTP is not referenced; authentication and admin reauthentication use local TOTP.
- Queue source URLs are neither typed for UI use nor rendered.

## Next step

Complete final validation and commit the query-navigation/theme restoration. Future web features must first be backed by an existing server endpoint or a separately approved server change.
