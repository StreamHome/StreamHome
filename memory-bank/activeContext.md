# Active Context

## Current focus

The approved web-only motion rebuild is implemented. StreamHome now uses responsive semantic timings, directional view transitions, staged local state choreography, asymmetric overlays and player controls, perceptible theme ambience, and application-wide reduced-motion behavior. Generated artwork paths are resolved through a shared cached probe before an image mounts, preventing repeated wrong-year 404 errors such as The Furious reporting 2026 while its physical folder is 2025. The server remains unchanged and is the source of truth for all media information.

## Current web architecture

- React routes are protected by hydrated auth and a query-aware profile guard that resolves `profile` against server profiles.
- API modules normalize the server's wire format into stable TypeScript models.
- The selected profile controls one of four canonical themes: Ember, Aurora, Cinema, or Gemini. Legacy, missing, and unknown values fall back to Ember; only explicit `cinema` selects Cinema.
- The authenticated application uses canonical query state such as `/?profile=1&view=series`; My List uses `view=watchlist`, and search, genres, details, seasons, playback, downloads, and admin sections remain deep-linkable.
- Shared catalog controller hooks own API behavior while a typed theme registry selects distinct Ember, Aurora, Cinema, and Gemini navigation, heroes, cards, details, and player presentation.
- Ember follows the Obsidian Frost reference with responsive glass-depth transitions, artwork drift, sharp glass, orange edge glow, scanlines, serif display type, and technical mono labels. Aurora, Cinema, and Gemini retain separate editorial, cinematic, and workspace billboard identities.
- A typed motion registry supplies semantic interaction, menu, dialog, view, billboard, artwork, list, and player-control timings with distinct Ember glass-depth, Aurora floating-blur, Cinema dissolve, and Gemini modular choreography.
- Query-backed dashboard content uses a 280ms directional exit and 520ms theme-specific entrance with coordinated scroll reset. Search, watchlist, episode, artwork, player, profile, and admin state changes animate locally instead of remounting the full page.
- Profile hover/focus uses an intent delay and crossfades the complete ambient background to the profile theme. Ember particles are frame-rate-independent, rise slowly, and all decorative backgrounds pause while hidden.
- Billboards track automatic/manual source and direction and transition over 1.05 seconds. Category arrows use an interruptible 760ms requestAnimationFrame controller with edge clamping and cancel on direct pointer, touch, or wheel input.
- Buttons preserve their existing colors on hover and use transform/shadow feedback. Reduced-motion mode uses short opacity fades and disables spatial motion and decorative animation.
- Movies and Series use rotating server-catalog billboards followed by per-genre horizontal rails. Rail controls are attached to the left/right edges and native horizontal scrollbars are hidden.
- The active-profile control opens Edit Profile, Switch Profile, and Sign Out actions. Its placement is surface-aware: the bottom Gemini desktop sidebar opens upward, while top and mobile navigation opens downward, with viewport-safe menu bounds. Editing can change the name or canonical theme without leaving the application; switching profiles remains explicit.
- The shared profile control intentionally contains only the avatar and profile name; the former caret `<i>` decoration is removed in every theme.
- The supplied StreamHome neon mark is a production web brand asset at `web/public/logo.png`. A shared `BrandLogo` component renders it on login, profile selection, all four theme navigation systems, Gemini compact/mobile navigation, and the admin header; the same asset is the browser favicon.
- `MediaArtwork` accepts direct server media paths and absolute HTTP(S) URLs. Compact filenames resolve through one shared cached range probe across generated `/media/...` candidates, mount only the valid image, crossfade after decode, and never substitute bundled media.
- The player resolves its movie or episode from authenticated server APIs and refuses playback when no physical media URL exists.
- Admin exposes only implemented server capabilities: current-account TOTP, storage/HEVC settings, and read-only download events.

## Security and data boundaries

- No media metadata or media files are stored in the web source tree.
- The ingestion API token is not exposed by the web client.
- SMTP/email OTP is not referenced; authentication and admin reauthentication use local TOTP.
- Queue source URLs are neither typed for UI use nor rendered.

## Next step

TypeScript lint, all 39 frontend tests, the production build, diff checks, and unauthenticated browser/console QA pass. Full authenticated browser QA and the required database checker remain unavailable because the existing local Python environment cannot start the server without `sqlmodel`.
