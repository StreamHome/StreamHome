# Playback

StreamHome uses a direct-media, verified-asset playback model. Pressing Play never starts completed-title transcoding. Completed ingestion schedules adaptive preparation after its catalog transaction commits, and startup recovery warms existing titles in bounded order with recently watched media first. The server creates one authenticated playback run, returns every currently playable URL and track, and lets the browser begin from the protected source or a verified adaptive stream immediately.

## Playback descriptor

`POST /api/playback/runs` returns one server-authoritative descriptor containing:

- the protected direct video URL;
- a protected HLS master only when its assets are already streamable;
- protected direct URLs for existing sibling `audio/` dubbing files;
- embedded audio identities that are actionable in the active transport;
- exact subtitle identities;
- the server-probed duration and saved profile position; and
- a stable same-origin `streamBaseUrl`, the legacy playback ticket, progress sequence, and next-episode identity.

Completed media is playback-ready when its original is measured as directly compatible or verified adaptive assets exist. An incompatible original waits for background preparation. Optional readiness refreshes never start generation or replace a healthy mounted transport. Missing HLS qualities are omitted instead of being shown as preparing or on demand.

## Direct video delivery

Browser-compatible media uses `GET|HEAD /api/playback/progressive/{mediaId}`. The endpoint supports `Range`, `206 Partial Content`, `Content-Range`, and bounded asynchronous reads. The browser requests the byte region it needs, so a seek does not transfer the preceding part of the title.

The video element uses eager browser preloading. Local media is read directly from `server/media`; Google Drive objects use bounded Rclone offset/count reads; validated HTTP sources use origin byte ranges. Raw filesystem paths, Drive paths, origin URLs, and ingestion headers never enter the browser contract.

## Ready HLS quality switching

If a protected HLS cache already contains a valid master, initialization fragments, playlists, and media fragments, the descriptor includes `/api/playback/manifest/{mediaId}`. A video rendition becomes ready only after FFprobe opens its completed playlist and confirms its stream. Adaptive audio is always rebased to zero before one server-owned relative offset is applied, then normalized to AAC-LC, 48 kHz, stereo with asynchronous timestamp repair. FFprobe must confirm that exact contract, structural audio/video starts must remain within 100 ms, their ends must remain within 250 ms, and FFmpeg must decode the complete audio playlist before publication. The 100 ms structural allowance accommodates frame-quantized container starts; deterministic decoded content markers enforce the 80 ms semantic synchronization boundary. Current verification markers are `.verified-v2` for video and `.verified-audio-v5` for audio. Older caches require background rebuilding before activation; a marker alone is never upgraded. AAC alignment uses decoded content origins, after codec priming and edit-list trimming, rather than a negative preroll packet. Signed HLS timestamps prevent the muxer from independently shifting video and audio. The player uses hls.js or native HLS to select only verified variants.

Quality clicks change the active ready HLS level. Native HLS selects a verified master variant with `rendition_id`, retaining its alternate audio group and the confirmed position/Play intent. They never call a preparation endpoint and never start FFmpeg. If no ready HLS master exists, the source plays directly and unavailable qualities are not displayed.

The adaptive forward-buffer target is 30 seconds with a bounded 60-second ceiling. Startup begins in automatic quality with a conservative bandwidth estimate. A saved manual level is restored only after at least eight seconds are buffered and measured bandwidth has 35 percent headroom; a viewer's explicit quality click still switches immediately.

## Startup path and diagnostics

The browser requests catalog metadata and the authenticated playback run concurrently, and mounts playable media as soon as the run resolves without waiting for catalog presentation data. It also begins loading hls.js as soon as the player route mounts, so API and player-engine work do not form a serial startup waterfall.

Adaptive startup records transport initialization, media attachment, manifest, level/audio playlist, fragment load/buffer, `canplay`, and `playing` stages. If the bounded retry cannot start playback, the client posts the exact last stage, sanitized hls.js error token, HTTP status, media element state, position, buffer edge, and elapsed time to `/api/playback/runs/{runId}/diagnostics`. The server writes this ticket-free diagnostic to `backend.log`, allowing an operator to distinguish a manifest failure, fragment HTTP failure, decode failure, or genuine no-progress timeout.

## External dubbing

Sibling files inside the title's `audio/` directory are direct playback assets. Each discovered external track receives a ticket-protected `/api/playback/source/{mediaId}?source_id=...` URL with the same range behavior as video.

The web player keeps a hidden audio element synchronized with the visible video. Selecting dubbing does not replace or reload the video. Ordinary Pause invalidates pending Play ownership and timers but does not call `load()`, change the source/preload contract, stop HLS loading, or reconstruct either media clock. Play, resume, seek, playback rate, volume, mute, completion, recovery, and teardown preserve video ownership. Track activation, the newest confirmed user seek, and metadata readiness explicitly align the audio clock. A denied audio Play request waits for another genuine user Play instead of retrying every animation frame. A short sidecar stays silent after its end. Persistent ordinary-playback drift at or above 80 ms may receive one hard correction per five-second cooldown. Every audio seek belongs to the active track, audio-source generation, transport generation, target, and deadline; stale or expired callbacks cannot settle or resume a newer transaction. If a sidecar cannot be decoded, the player restores the default embedded audio without changing the video position.

Standalone dubbing files have independent container epochs. Their FFprobe `start_time` is retained only as diagnostic metadata and is not subtracted from the video's start time. Embedded streams in the same container may carry a server-owned video-relative offset. Adaptive generation rebases the selected audio once and applies that authoritative offset once.

A ready HLS video can also use a direct external dubbing sidecar when that audio is not inside the manifest.

## Resume and seeking

The saved position is included in the initial descriptor and applied after media metadata is available but before playback begins. The player never intentionally starts at zero and corrects the position later.

Direct seeking assigns the requested `video.currentTime`, which causes a new byte-range request near that position. Ready HLS seeking asks hls.js for the containing prepared range. The newest requested position remains authoritative until a target-local `timeupdate` or `seeked` confirms it. Out-of-order rapid-seek events, late transport readiness, and unsolicited backward jumps cannot reset the stable clock. A genuine native `seeking` command acquires its own transaction, including backward seeks; external audio aligns only after that newest target settles.

The server-probed duration remains authoritative. Buffered ranges and HLS playlist growth cannot shorten the visible title duration.

## Buffering and recovery

The browser keeps upcoming direct media buffered according to its media engine. hls.js targets 30 seconds ahead and may grow to 60 seconds when bandwidth permits. The timeline reports the currently buffered range.

Temporary network or source stalls retain the last decoded frame. Clock monitoring continues after startup, and decoded-frame callbacks also detect a picture freeze while the clock advances. Background visibility is excluded and monitoring resumes when visible. After four seconds without future media, the player reloads the same transport from the confirmed position. Two bounded recovery attempts are allowed before an actionable error is shown. HLS retains its bounded network and media recovery budgets. External audio may buffer and recover independently, but it never pauses or restarts the master video and never owns the global buffering state.

## Ingestion preview

Play While Downloading remains a separate exception. An active ingestion can expose its growing, task-scoped HLS preview when enough media exists. The submitted source URL and headers remain server-only. Existing preview runs survive catalog handoff; new completed-media runs use direct video plus any already-ready HLS assets.

## Authentication

Playback remains restricted to a signed-in session and selected profile. Tickets are scoped to the authentication session, profile, playback run, media identity, and source fingerprint. Browser assets use `GET|HEAD /api/playback/streams/{runId}/{asset}` with the existing signed-in session cookie. The server rechecks the session, profile, run and source on every asset, then applies the protected delivery contract with a short internal ticket. No reusable credential enters the stable URL. Credential/readiness renewal preserves the mounted decoder, buffer, position and explicit Pause. Legacy ticket URLs remain supported for integration consumers. Access logging redacts the ticket query value before formatting any protected playback request.

## Controls, captions and progress

Primary controls are Play/Pause, seeking, time, volume and fullscreen. Audio,
subtitles and ready quality remain directly accessible. Settings groups speed,
caption size and timing. One menu owner controls popovers; they are mounted outside
scrolling control rows and constrained to the player viewport. Native button/menu
keyboard behavior and browser modifier shortcuts remain available. A rotated phone
timeline uses its screen Y axis. Buffering, recovery, Pause, menus and scrubbing keep
controls visible, and Tab reveals hidden controls before focus moves.

Scrub preview never changes the confirmed playback clock or watched-time counter.
Captions use `cuechange` and indexed media-time activation, retaining safe WebVTT
emphasis. hls.js subtitle controllers are disabled because the application owns the
server-provided tracks. Document picture-in-picture retains the existing video and
app captions; video-only native surfaces use the selected native track with a
reversible offset. Support for those native surfaces still depends on the browser.

Progress optionally carries `total_seconds_played`, a cumulative whole-second count
for that run. Server writers serialize progress, Close and Replay, apply the delta
once, and retain delayed snapshots within the run's elapsed-time bound. Legacy
`duration_watched` remains supported. Replay shares the client queue and blocks old
snapshots while pending; explicit Pause during Replay remains authoritative.
`discard_progress` on Close abandons an obsolete, never-mounted or cancelled run
without overwriting a newer profile position. BFCache restoration creates a fresh
run while preserving the prior Play/Pause intent.

## Validation

Playback coverage verifies:

- exact local and validated HTTP source byte ranges;
- authenticated direct video and external dubbing byte ranges;
- ready-HLS manifest and static fragment allowlisting;
- FFprobe-verified rendition publication and interrupted-cache recovery;
- absence of JIT routes and Play-triggered FFmpeg work;
- startup catalog warming and completed-ingestion scheduling;
- concurrent startup requests, transport-stage diagnostics, and bandwidth-safe initial quality;
- resume and source-fingerprint ticket invalidation;
- direct external dubbing selection without video replacement;
- content-level flash/impulse synchronization with a deliberately shifted control fixture on Linux;
- source-stable Pause/Play and generation-owned external-audio seek settlement;
- bounded forward buffering and stall recovery;
- progress, close, subtitles, previews, fullscreen, and mounted player behavior; and
- real prepared HLS video/audio compatibility for retained ready assets.


Browser regression coverage lives in `web/e2e/player.remediation.spec.ts` alongside
the existing visual fixture suite. `STREAMHOME_PLAYER_PERF=1` enables the controlled
ready-local-media latency gates; it is not a WAN or device-wide guarantee.
`STREAMHOME_SEMANTIC_FIXTURE=1` additionally requires the exported synthetic HLS
fixture at `web/test-assets/player-semantic-hls`. Generate it only in a disposable
installation with the backend flash/impulse test's optional
`STREAMHOME_PLAYER_FIXTURE_EXPORT` path under that installation's `web/test-assets`.
Never run stateful backend fixture preparation against an existing valuable install.
A missing/invalid browser capture is incomplete evidence, not a synchronization pass.
