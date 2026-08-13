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
- the playback ticket, progress sequence, and next-episode identity.

Completed media is always reported as playback-ready when its direct source is available. The player does not poll for rendition preparation. Missing HLS qualities are omitted instead of being shown as preparing or on demand.

## Direct video delivery

Browser-compatible media uses `GET|HEAD /api/playback/progressive/{mediaId}`. The endpoint supports `Range`, `206 Partial Content`, `Content-Range`, and bounded asynchronous reads. The browser requests the byte region it needs, so a seek does not transfer the preceding part of the title.

The video element uses eager browser preloading. Local media is read directly from `server/media`; Google Drive objects use bounded Rclone offset/count reads; validated HTTP sources use origin byte ranges. Raw filesystem paths, Drive paths, origin URLs, and ingestion headers never enter the browser contract.

## Ready HLS quality switching

If a protected HLS cache already contains a valid master, initialization fragments, playlists, and media fragments, the descriptor includes `/api/playback/manifest/{mediaId}`. A video rendition becomes ready only after FFprobe opens its completed playlist and confirms its stream. Adaptive audio is always normalized to AAC-LC, 48 kHz, stereo with repaired zero-based timestamps; FFprobe must confirm that exact contract, its start position must remain within 250 ms of a verified video rendition, and FFmpeg must decode the complete audio playlist before publication. Independently versioned audio verification invalidates legacy stream-copied audio without rebuilding verified video. The player uses hls.js or native HLS to select only verified variants.

Quality clicks change the active ready HLS level. They never call a preparation endpoint and never start FFmpeg. If no ready HLS master exists, the source plays directly and unavailable qualities are not displayed.

The adaptive forward-buffer target is 30 seconds with a bounded 60-second ceiling. Startup begins in automatic quality with a conservative bandwidth estimate. A saved manual level is restored only after at least eight seconds are buffered and measured bandwidth has 35 percent headroom; a viewer's explicit quality click still switches immediately.

## Startup path and diagnostics

The browser requests catalog metadata and the authenticated playback run concurrently. It also begins loading hls.js as soon as the player route mounts, so API and player-engine work do not form a serial startup waterfall.

Adaptive startup records transport initialization, media attachment, manifest, level/audio playlist, fragment load/buffer, `canplay`, and `playing` stages. If the bounded retry cannot start playback, the client posts the exact last stage, sanitized hls.js error token, HTTP status, media element state, position, buffer edge, and elapsed time to `/api/playback/runs/{runId}/diagnostics`. The server writes this ticket-free diagnostic to `backend.log`, allowing an operator to distinguish a manifest failure, fragment HTTP failure, decode failure, or genuine no-progress timeout.

## External dubbing

Sibling files inside the title's `audio/` directory are direct playback assets. Each discovered external track receives a ticket-protected `/api/playback/source/{mediaId}?source_id=...` URL with the same range behavior as video.

The web player keeps a hidden audio element synchronized with the visible video. Selecting dubbing does not replace or reload the video. Play, pause, resume, seek, playback rate, volume, mute, completion, recovery, and teardown preserve video ownership. Track activation, the newest confirmed user seek, and metadata readiness explicitly align the audio clock. Ordinary playback synchronization does not seek; severe drift of at least one second may receive one hard correction per five-second cooldown. If a sidecar cannot be decoded, the player restores the default embedded audio without changing the video position.

A ready HLS video can also use a direct external dubbing sidecar when that audio is not inside the manifest.

## Resume and seeking

The saved position is included in the initial descriptor and applied after media metadata is available but before playback begins. The player never intentionally starts at zero and corrects the position later.

Direct seeking assigns the requested `video.currentTime`, which causes a new byte-range request near that position. Ready HLS seeking asks hls.js for the containing prepared range. The newest requested position remains authoritative until a target-local `timeupdate` or `seeked` confirms it. Out-of-order rapid-seek events, late transport readiness, and unsolicited backward jumps cannot reset the stable clock; external audio aligns only after that newest target settles.

The server-probed duration remains authoritative. Buffered ranges and HLS playlist growth cannot shorten the visible title duration.

## Buffering and recovery

The browser keeps upcoming direct media buffered according to its media engine. hls.js targets 30 seconds ahead and may grow to 60 seconds when bandwidth permits. The timeline reports the currently buffered range.

Temporary network or source stalls retain the last decoded frame. After four seconds without future media, the player reloads the same transport from the confirmed position. Two bounded recovery attempts are allowed before an actionable error is shown. HLS retains its bounded network and media recovery budgets. External audio may buffer and recover independently, but it never pauses or restarts the master video and never owns the global buffering state.

## Ingestion preview

Play While Downloading remains a separate exception. An active ingestion can expose its growing, task-scoped HLS preview when enough media exists. The submitted source URL and headers remain server-only. Existing preview runs survive catalog handoff; new completed-media runs use direct video plus any already-ready HLS assets.

## Authentication

Playback remains restricted to a signed-in session and selected profile. Tickets are scoped to the authentication session, profile, playback run, media identity, and source fingerprint. Direct video, direct dubbing, ready HLS, preview assets, and subtitles validate the ticket. Access logging redacts the ticket query value before formatting any protected playback request.

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
- bounded forward buffering and stall recovery;
- progress, close, subtitles, previews, fullscreen, and mounted player behavior; and
- real prepared HLS video/audio compatibility for retained ready assets.
