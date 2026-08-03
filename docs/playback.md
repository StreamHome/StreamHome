# Playback

StreamHome uses a direct-first playback engine with time-indexed just-in-time HLS fallback. Completed media becomes playable as soon as its source is verified and probed. Publication and player startup never wait for a complete set of transcoded qualities or audio tracks.

## Delivery selection

The browser receives two authenticated transports for completed catalog media:

- a progressive endpoint that supports `HEAD`, `Range`, `206`, and `Content-Range`; and
- a JIT HLS master whose variant and audio playlists describe the complete media timeline immediately.

The player starts browser-compatible source media through the progressive endpoint. This is the normal low-latency HTML video path and requires no FFmpeg work. If the source codec/container is incompatible, the saved audio preference requires adaptive delivery, or the viewer selects a quality or dubbing track, the player attaches hls.js at the current stable timestamp.

The existing player presentation remains unchanged. Direct and adaptive transports share one play/pause intent, stable playback clock, resume position, last-frame overlay, retry budget, progress session, subtitles, and fullscreen controller.

## Seekable sources

`services/playback_source.py` provides one random-access source contract:

- `LocalPlaybackSource` performs bounded asynchronous file reads.
- `DrivePlaybackSource` performs rclone offset/count reads for browser delivery. FFmpeg receives a loopback-only authenticated range URL, allowing its demuxer to issue byte ranges instead of consuming `rclone cat` from byte zero.
- `HttpPlaybackSource` validates source URLs and approved headers, revalidates every redirect, probes range support, and streams only the requested origin bytes.

Raw source URLs, Drive paths, request headers, and filesystem paths are never included in the browser playback contract.

## Time-indexed adaptive delivery

JIT media playlists are VOD playlists that cover the complete server-owned duration before any segment exists. Each segment number maps directly to a timestamp:

```text
start time = segment number × PLAYBACK_SEGMENT_SECONDS
```

A request for a segment around one hour starts FFmpeg around one hour. It does not generate the preceding hour. The server generates a bounded forward window, publishes the requested MPEG-TS segment as soon as that individual file is complete, and continues producing nearby reusable segments in the background.

Video renditions use H.264 High Profile output with aligned forced keyframes. Audio renditions use aligned AAC stereo output. Every video quality and embedded/external audio rendition shares the same segment grid, so hls.js can switch at a nearby boundary without rebuilding the playback run.

Generation jobs are deduplicated by media fingerprint, rendition, and window number. The segment cache is bounded by the existing playback cache size and evicted by least-recently-used media fingerprint. Active cache roots are not removed. Segment responses expose `Server-Timing` and an `X-StreamHome-Playback-Cache` hit/miss header for measurement.

Default controls are:

- `PLAYBACK_SEGMENT_SECONDS=4`
- `PLAYBACK_WINDOW_SEGMENTS=6`
- `PLAYBACK_SEGMENT_WAIT_SECONDS=30`
- `PLAYBACK_TRANSCODE_CONCURRENCY=2`
- `PLAYBACK_CACHE_GB=20`
- `PLAYBACK_LOOPBACK_URL=http://127.0.0.1:8000`

## Resume and seeking

The saved profile position is returned with the playback run. Direct playback applies it after metadata is available and before playback begins. Adaptive playback supplies it to hls.js as `startPosition`, causing hls.js to request the containing JIT segment immediately.

For later seeks, the application clock moves to the requested value immediately. Direct playback assigns `video.currentTime`, allowing the browser to issue new byte ranges. Adaptive playback asks hls.js to start loading at the requested position. When the target range becomes seekable, the video element commits that exact target. The last decoded frame remains visible during the bounded transition.

Movie duration is always server-owned. A partially buffered source or generated segment window cannot redefine the full runtime.

## Quality and audio switching

Playback-run quality and audio lists describe source capabilities, not cache contents. A valid capability is actionable immediately and is never disabled as `Preparing`.

When adaptive playback is active, quality selection sets the hls.js level and audio selection sets its audio-track index. When direct playback is active, selecting a derived quality, non-default embedded track, or external dubbing track moves to the JIT master at the current timestamp and applies the requested selection as soon as hls.js attaches.

Auto quality remains hls.js adaptive bitrate selection. The source rendition is labeled Original, and derived levels never upscale beyond the source dimensions.

## Ingestion and startup

Ingestion now publishes after the output is verified, probed, cataloged, and assigned a source fingerprint. Cloud publication waits for the verified upload identity, but it does not generate playback renditions. Server startup repairs catalog metadata and source identities without warming the entire library.

The legacy pre-generated fMP4 cache can still serve an already issued legacy URL during migration, but no new completed-media run, ingestion task, startup job, quality selection, or audio selection depends on it.

## Authentication

Playback remains restricted to a signed-in session and selected profile. Playback tickets are scoped to the authentication session, playback run, media identity, and source fingerprint. Manifests, segments, progressive ranges, source bridges, and subtitles validate the ticket. This is an access-control boundary, not DRM; the delivery architecture does not sacrifice startup or seeking performance to obscure media bytes.

## Play While Downloading

Active MediaSender ingestion continues to use an application-owned growing HLS preview. The submitted source URL and headers remain server-only. Existing preview runs survive final catalog handoff, while new runs use direct/JIT completed-media delivery.

A downloading preview can only seek through bytes already processed by ingestion. Once publication completes, the full timeline becomes immediately seekable through the completed source.

## Validation

Playback regression coverage verifies:

- exact local and HTTP origin byte ranges;
- authenticated progressive and FFmpeg source-bridge ranges;
- full-timeline virtual manifests before segment generation;
- generation at a nonzero requested window without earlier segments;
- aligned video and audio segment production;
- cached segment reuse and timing headers;
- resume preservation across playback-run refresh;
- immediate quality and external-audio capability selection;
- source-fingerprint ticket invalidation;
- real mounted `PlayerPage` seeking and transport transitions; and
- bounded recovery, fullscreen, progress, subtitle, and ingestion-preview behavior.
