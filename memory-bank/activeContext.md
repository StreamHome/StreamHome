# Active Context

## Current focus

The semantic hover interaction rebuild, full-page themed profile editor, and web motion system are implemented. The previous universal scale effect and competing card transforms are gone; fine-pointer hover now uses responsive enter/exit timing, theme-specific card and navigation behavior, explicit coverage for all major controls, keyboard focus parity, and reduced-motion fallbacks. Profile editing remains on the authenticated, return-aware `/profiles/:profileId/edit` route. The server remains unchanged and is the source of truth for all media information.

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
- A dedicated interaction stylesheet owns hover behavior instead of scattered utility classes or blanket transforms. Actions use restrained lift/light feedback; cards coordinate surface elevation, artwork parallax, edge illumination, and copy motion without full-card scaling. Ember uses technical edge glow, Aurora glass bloom, Cinema poster depth/red accents, and Gemini modular elevation/gradient accents.
- Navigation, profile menus, search, filters, rails, pagination, details, episodes, profile selection/editor, admin, dialogs, login, player, and recovery controls all have explicit hover contracts. Spatial hover runs only on fine pointers; keyboard focus keeps equivalent clarity without movement, and reduced-motion mode disables spatial/sheens while retaining short visual feedback.
- Movies and Series use rotating server-catalog billboards followed by per-genre horizontal rails. Rail controls are attached to the left/right edges and native horizontal scrollbars are hidden.
- The active-profile control opens Edit Profile, Switch Profile, and Sign Out actions. Its placement is surface-aware: the bottom Gemini desktop sidebar opens upward, while top and mobile navigation opens downward, with viewport-safe menu bounds. Every Edit Profile entry point opens the dedicated editor and preserves its exact originating URL for Save, Cancel, and deletion return behavior.
- The profile editor owns draft name, theme, and avatar state. Theme changes preview only inside the editor until Save; saving the active profile then synchronizes the global theme while preserving existing PIN fields. Unknown legacy avatar values render a deterministic allowlisted fallback, so arbitrary stored CSS is never applied.
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

TypeScript lint, all 55 frontend tests, the production build, and diff checks pass. Browser QA confirms the interaction stylesheet loads without application errors, fine-pointer detection is active, and reduced-motion disables spatial hover as designed. Normal-motion and authenticated four-theme live hover QA remain unavailable in the current saved browser/session; the required database checker remains blocked because the local Python environment lacks `sqlmodel`.
