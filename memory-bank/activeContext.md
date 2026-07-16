# Active Context

## Current focus

The current web-only rebuild adds a shared cinematic motion contract to Ember, Aurora, Cinema, and Gemini without replacing their established layouts. Profile entry, query views, billboards, category rails, menus, dialogs, backgrounds, player controls, and hover states now use deliberate theme-specific motion. The server remains unchanged and is the source of truth for all media information.

## Current web architecture

- React routes are protected by hydrated auth and a query-aware profile guard that resolves `profile` against server profiles.
- API modules normalize the server's wire format into stable TypeScript models.
- The selected profile controls one of four canonical themes: Ember, Aurora, Cinema, or Gemini. Legacy, missing, and unknown values fall back to Ember; only explicit `cinema` selects Cinema.
- The authenticated application uses canonical query state such as `/?profile=1&view=series`; My List uses `view=watchlist`, and search, genres, details, seasons, playback, downloads, and admin sections remain deep-linkable.
- Shared catalog controller hooks own API behavior while a typed theme registry selects distinct Ember, Aurora, Cinema, and Gemini navigation, heroes, cards, details, and player presentation.
- Ember follows the Obsidian Frost reference with slow crossfades, artwork drift, sharp glass, orange edge glow, scanlines, serif display type, and technical mono labels. Aurora, Cinema, and Gemini retain separate editorial, cinematic, and workspace billboard identities.
- A typed motion registry supplies cinematic-slow timings and distinct Ember glass-depth, Aurora floating-blur, Cinema dissolve, and Gemini modular choreography while the shared compatibility adapter preserves non-Ember layouts.
- Query-backed dashboard content now uses a clearly visible two-second sequence: an 800ms exit, coordinated scroll reset, and 1200ms theme-specific entrance. Player and admin views receive the same query-driven behavior without delaying URL updates.
- Profile hover/focus uses an intent delay and crossfades the complete ambient background to the profile theme. Ember particles are frame-rate-independent, rise slowly, and all decorative backgrounds pause while hidden.
- Billboards track automatic/manual source and direction and transition over 2.3 seconds. Category arrows use a cancelable 1500ms requestAnimationFrame controller with edge clamping instead of browser-defined smooth scrolling.
- Buttons preserve their existing colors on hover and use transform/shadow feedback. Reduced-motion mode uses short opacity fades and disables spatial motion and decorative animation.
- Movies and Series use rotating server-catalog billboards followed by per-genre horizontal rails. Rail controls are attached to the left/right edges and native horizontal scrollbars are hidden.
- The active-profile control opens Edit Profile, Switch Profile, and Sign Out actions. Its placement is surface-aware: the bottom Gemini desktop sidebar opens upward, while top and mobile navigation opens downward, with viewport-safe menu bounds. Editing can change the name or canonical theme without leaving the application; switching profiles remains explicit.
- The supplied StreamHome neon mark is a production web brand asset at `web/public/logo.png`. A shared `BrandLogo` component renders it on login, profile selection, all four theme navigation systems, Gemini compact/mobile navigation, and the admin header; the same asset is the browser favicon.
- `MediaArtwork` accepts direct server media paths and absolute HTTP(S) URLs. Compact server artwork filenames are resolved against the server-returned movie or episode identity into `/media/...` candidates; it never substitutes a bundled media image.
- The player resolves its movie or episode from authenticated server APIs and refuses playback when no physical media URL exists.
- Admin exposes only implemented server capabilities: current-account TOTP, storage/HEVC settings, and read-only download events.

## Security and data boundaries

- No media metadata or media files are stored in the web source tree.
- The ingestion API token is not exposed by the web client.
- SMTP/email OTP is not referenced; authentication and admin reauthentication use local TOTP.
- Queue source URLs are neither typed for UI use nor rendered.

## Next step

The final lint, 38-test suite, and production build pass. No code implementation follow-up is pending for the brand integration, menu placement, or extended timing repair. Full authenticated browser QA and the required database checker remain unavailable because the existing local Python environment cannot start the server without `sqlmodel`.
