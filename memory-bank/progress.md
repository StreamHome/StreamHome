# Progress Status

## Active comprehensive motion rebuild

- [x] Removed the shared profile-control caret `<i>` decoration and its obsolete CSS from Ember, Aurora, Cinema, and Gemini navigation.

- [x] Audited route, component-state, data-mutation, navigation, artwork, details, playback, profile, authentication, and admin transitions across all four themes.
- [x] Replaced the globally overextended timing scale with semantic interaction, overlay, page, billboard, list, artwork, and player-control timings.
- [x] Added application-wide Framer Motion reduced-motion configuration, directional page variants, shared content reveal variants, and a reusable animated state boundary.
- [x] Added a persistent authenticated surface transition boundary so dashboard, player, and admin can complete coordinated exits and entrances.
- [x] Restored responsive semantic theme timing variables, animated navigation state changes, shortened billboard-copy timing, and removed competing CSS transforms.
- [x] Split Ember pointer tilt from card lift so hover interactions remain immediate without transform-transition conflicts.
- [x] Added interruptible, more balanced rail scrolling and server-artwork decode reveals with reduced-motion fallbacks.
- [x] Added staged catalog collection entry, animated search-result replacement, watchlist empty/populated transitions, and limited full-page transitions to true view changes.
- [x] Added cinematic details artwork/copy reveals, animated watchlist feedback, and season-aware episode loading, empty, and grid transitions.
- [x] Added player surface entry, buffering feedback, skip-marker and completion overlays, plus rescheduled asymmetric control reveals and exits.
- [x] Added reduced-motion-aware profile ambient morphing and animated admin navigation/panel replacement.
- [x] Added animated password-to-TOTP reauthentication steps and authentication-surface entry.
- [x] Rebalanced theme ambience: Aurora and Cinema now move perceptibly, Ember particles have visible drift, and Gemini uses frame-rate-independent timing.
- [x] Brought Ember catalog rails, search results, and watchlist lifecycle states into the shared staged-motion system.
- [x] Updated motion tests to enforce responsive semantic timings, asymmetric overlays/controls, and direction-resolved per-theme choreography.
- [x] Added expanding TOTP setup and animated account/storage success-error feedback in the admin panels.
- [x] Reworked dropdowns, generic modals, profile settings, and glass spotlights around responsive asymmetric overlay timing.
- [x] Replaced repeated generated-artwork `<img>` 404 retries with a shared cached server-path resolver and reduced-motion-safe loading shimmer.
- [x] Added artwork resolver tests for wrong-year fallback selection and cross-component request deduplication.
- [x] Wired theme-specific and local content choreography through catalog, details, player, profiles, authentication, and admin surfaces.
- [x] Expanded behavior-focused coverage to 39 passing tests; TypeScript lint, production build, diff checks, and unauthenticated browser/console QA pass.
- [x] The required database checker was attempted and is blocked by the existing local Python environment missing `sqlmodel`; the web-only change does not modify server behavior.

## Server

- [x] FastAPI, SQLModel/SQLite, FFmpeg, TMDB enrichment, download queue processing, local/cloud storage settings, recovery scanning, and range streaming are implemented.
- [x] The database path is standardized to the absolute `server/database.db` path resolved from `server/config.py`.
- [x] Physical media, artwork, subtitles, and `.metadata/metadata.json` records live under `server/media`; FastAPI exposes that catalog through `/media` and authenticated API routes.
- [x] Movie ingestion excludes `season` and `episode`; series ingestion supplies them when applicable.
- [x] Authentication uses password plus optional local TOTP. SMTP and email OTP support are removed.
- [x] Download progress is available through the authenticated SSE queue endpoint.
- [x] Storage and background HEVC settings are available through the server settings API.

The server was deliberately not changed during the current web repair. Existing detailed server behavior remains documented in the server code and `memory-bank/mediaSenderAPI.md`.

## Ember Obsidian Glass rebuild

- [x] Approved implementation plan covers the dedicated Ember component tree, legacy-theme compatibility adapter, profile editing/deletion, view scroll restoration, and episode URL synchronization.
- [x] Legacy `netflix`, missing, and unknown profile themes now resolve to Ember; explicit canonical theme identifiers remain unchanged.
- [x] Authenticated theme state is no longer written to the document root, protecting standalone pages from theme leakage.
- [x] Added shared profile update/removal state operations and the frontend profile-save API contract.
- [x] Added query-view scroll restoration at the authenticated application boundary.
- [x] Added server-backed create/edit/delete profile management with all four canonical theme choices, typed-name deletion confirmation, and administrator deletion protection.
- [x] Added a typed theme application contract: Ember owns a dedicated component tree while Aurora, Cinema, and Gemini continue through their unchanged compatibility adapter.
- [x] Rebuilt Ember home, archive, search, details, downloads, loading, error, empty, and unavailable states with the supplied Obsidian Glass visual language and responsive navigation.
- [x] Series playback now resolves the ordered server episode list, auto-advances to the next playable episode, and replaces the URL `media` parameter without adding a redundant history entry.
- [x] Decorative Ember animation pauses while the document is hidden and respects reduced-motion preferences.

## System-wide billboard and discovery rebuild

- [x] Added `view=watchlist` to canonical query routing and navigation for Ember, Aurora, Cinema, and Gemini.
- [x] Added server-order My List presentation with separate Movies and Series rails.
- [x] Replaced Movies/Series archive headings and undifferentiated grids with rotating featured billboards plus server-genre collections in all themes.
- [x] Added edge-attached rail controls, hidden horizontal scrollbars, stable keyboard focus, and repaired card hover/artwork motion.
- [x] Added an active-profile menu with Edit Profile, Switch Profile, and Sign Out; profile edits remain server-backed and can change any canonical theme.
- [x] Preserved administrator navigation in every theme, including when the administrator profile switches to Aurora.
- [x] Limited billboard rotation to eight server records, pauses rotation on hover/focus or hidden documents, and respects reduced-motion preferences.
- [x] Corrected global page overflow so the document is the scroll container, and made view restoration reset both document and application-root positions.
- [x] Preserved theme identities: Ember Obsidian Glass, Aurora editorial glass, Cinema cinematic backdrop, and Gemini modular workspace.

## Server artwork compatibility

- [x] Kept every poster, backdrop, thumbnail, and artwork metadata record server-owned; no media assets were added to the web.
- [x] Added a web resolver for compact server references such as `/poster.jpg`, `/backdrop.jpg`, and `/thumbnail.jpg`.
- [x] Artwork candidates are derived only from the current server-returned media identity and resolve through `/media/...` for movies, series, and episodes.
- [x] `MediaArtwork` retries canonical server paths, including limited catalog/folder year drift, before rendering the unavailable state.
- [x] Wired server media context through Ember and the Aurora/Cinema/Gemini compatibility application, details views, search results, and episode cards.

## Previous web repair baseline

- [x] Replaced optimistic/mismatched client types with normalization at the API boundary for auth, movies, episodes, playback, profiles, settings, and queue events.
- [x] Fixed auth hydration, TOTP response-key normalization, and profile restoration. The former `netflix` to Cinema compatibility rule has now been superseded by the Ember fallback required by the approved rebuild.
- [x] Removed all bundled poster/backdrop assets and mock media fallbacks. The web client now displays only server-provided media URLs; absent or unusable artwork becomes a neutral CSS placeholder.
- [x] Replaced the generic dashboard shell with shared server-data controllers and four distinct presentation systems: Obsidian Frost Ember, editorial Aurora, cinematic Cinema, and workspace Gemini.
- [x] Added canonical query navigation for `profile`, `view`, `media`, `genre`, `season`, `q`, and admin `section`, including validation, legacy redirects, refresh restoration, and browser history.
- [x] Implemented working catalog tabs, search, watchlist, details, TV season/episode selection, profile switching, and read-only download progress.
- [x] Rebuilt the player around server catalog records, authenticated stream URLs, quality selection, playback reporting, subtitles, and skip markers. Records without playable media are visibly unavailable.
- [x] Reduced the web admin center to supported server capabilities: Account/TOTP, Storage/HEVC, and read-only Downloads. Removed unsupported backup, update, user mutation, source URL, and queue-cancel controls.
- [x] Added an application error boundary and focused Vitest coverage for API normalization, playback payloads, store migration/hydration, and media URL filtering.

## Cinematic motion rebuild

- [x] Added a typed, shared motion registry with cinematic-slow timings and distinct view, billboard, and card choreography for Ember, Aurora, Cinema, and Gemini.
- [x] Added a central normal/reduced-motion and document-visibility provider; reduced mode now uses minimal fades instead of collapsing every transition to `0.01ms`.
- [x] Added profile hover intent, full ambient theme morphing, staggered profile entry, animated profile selection, and animated create/edit/delete dialogs.
- [x] Added animated profile menus and preserved the explicit Edit Profile, Switch Profile, and Sign Out behavior.
- [x] Made Ember particles time-based at 3–7 pixels per second and repaired hidden-document suspension in Ember and Gemini; Aurora, Cinema, and Gemini now use substantially slower visibility-aware ambience.
- [x] Added coordinated query-view exits, scroll reset, and theme-specific entries without remounting shared catalog controllers or changing query URLs. The sequence now lasts two seconds: 800ms out and 1200ms in.
- [x] Added direction-aware, overlapping 2.3-second billboard transitions with slower staged copy for all themes.
- [x] Replaced native smooth rail scrolling with a cancelable, edge-clamped 1500ms controller and direction feedback.
- [x] Replaced authenticated button hover color swaps with slow scale/lift/shadow feedback and retained semantic active/focus/disabled colors.
- [x] Slowed player controls, shared modals, dropdowns, Gemini navigation collapse, and glass hover feedback.
- [x] Added deterministic tests for motion timings, theme completeness, normal/reduced preferences, billboard rotation/direction/pause, rail easing/clamping, and button hover contracts.
- [x] Corrected profile-menu placement so Gemini desktop opens upward from its bottom sidebar while mobile Gemini and top navigation open downward; menus are viewport-bounded and internally scroll when necessary.
- [x] Slowed hover, menu, dialog, profile handoff, theme morph, ambient, player, and navigation-collapse animation across all four themes.
- [x] Added component coverage for desktop and mobile profile-menu placement and expanded timing assertions for the two-stage page transition.
- [x] Live browser QA confirmed no horizontal overflow at 1280px, 820px, and 390px login layouts, a stable reduced-motion frame, and no console warnings.
- [ ] Authenticated four-theme live animation QA requires a runnable local FastAPI environment; both available Python runtimes currently fail before startup because `sqlmodel` is missing.

## Validation

- [x] `npm run lint`
- [x] `npm test` (17 files, 38 tests)
- [x] `npm run build`
- [x] Live browser validation of Ember, Aurora, Cinema, and Gemini billboards, genre rails, My List, profile settings menu/dialog, server artwork, query navigation, root/document scroll restoration, desktop/mobile overflow, and console output.
- [x] Live browser validation against the local server: Ember desktop/mobile home, movies, details, series-empty, downloads-empty, profile gallery/settings, safe create/delete, query navigation, and scroll restoration.
- [x] Created and removed a temporary Gemini profile during QA to verify the Aurora/Cinema/Gemini compatibility application remains intact.
- [x] `server/scratch/check_db.py` (33 catalog records, 0 episodes, 0 playback sessions).
- [x] Production web tree scan found no bundled media files, metadata records, or temporary QA artifacts.
- [x] Artwork coverage check resolved all 66 poster/backdrop files for all 33 current catalog records; every file hash is distinct and none are missing.
- [x] The earlier artwork pass visually confirmed that the resolved server artwork files are valid and distinct; the current rebuild additionally rendered those server files in live browser QA.
- [ ] The required `server/scratch/check_db.py` could not run in this environment because both installed Python interpreters are missing `sqlmodel`; no server code or database files were changed.

## StreamHome logo integration

- [x] Added the supplied 1024px PNG as the production brand asset at `web/public/logo.png` and configured it as the browser favicon.
- [x] Added a reusable, accessible `BrandLogo` component with full-wordmark and compact-mark modes.
- [x] Integrated the logo into login, profile selection, Ember, Aurora, Cinema, Gemini desktop/mobile navigation, Gemini collapsed navigation, and the admin header.
- [x] Added responsive logo sizing and retained existing text wordmarks where space allows.
- [x] Added focused component tests for the public asset path and compact-mark accessible name.

## Remaining server backlog

- [ ] Expose rclone upload progress in a supported server API before adding it to the web UI.
- [ ] Add server-side subtitle conversion only if it remains a product requirement.
