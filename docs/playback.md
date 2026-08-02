# Playback

StreamHome uses protected adaptive fMP4 HLS as its primary playback transport for completed local and Google Drive media.

Playback behavior includes:

- playback-run-scoped HLS manifests and segments;
- ingestion-time and startup playback pre-generation;
- adaptive video variants;
- multiple audio tracks and subtitles;
- profile-specific resume state;
- cleanup of expired playback runs and cache artifacts.

## Complete adaptive preparation

Ingestion keeps a movie or episode in `processing` state until every source-bounded video quality and every embedded/external audio rendition is complete. Local ingestion prepares directly from the retained catalog source. Cloud ingestion prepares from verified local staging, uploads the media, migrates the complete cache to the verified remote fingerprint, and only then removes staging and publishes the catalog entry as available.

Server startup reconciles source identities and warms older catalog entries one at a time, prioritizing recently watched media. Complete markers make this work restart-safe: already finished renditions are reused, and only missing renditions are scheduled. Cold playback and explicit retry paths also schedule the complete set, so legacy items converge to the same prepared state.

Compatible H.264 video and AAC audio are packaged into fMP4 HLS without re-encoding. Sources that are not browser-compatible receive browser-compatible H.264/AAC renditions. A growing protected master may be published for status inspection, but completed-media playback does not begin until every rendition has an end-to-end complete marker. This guarantees the saved position and arbitrary timeline seeks are valid before the first frame.

The playback-run contract reports `seekableUntil`, `resumeReady`, `switchingReady`, and `fullyPrepared`. The player polls through both preparing and partially streamable states, preserves the saved position without temporarily resetting to zero, and mounts its transport only when `fullyPrepared` is true. Unfinished quality/audio options are disabled as `Preparing`; ready hls.js levels and audio tracks switch in the active transport without an API preparation request or full player reload.

FFmpeg jobs that stop producing output are terminated with a specific preparation failure instead of leaving an indefinite spinner. Secondary-rendition failures are surfaced as preparation errors because complete readiness cannot otherwise be reached. Backend logs record per-rendition preparation/seekable timing, total ingestion preparation time, and playback-run cache readiness.

For cloud-only media, a fully prepared application cache is a playback-run fast path and avoids repeated Google Drive metadata probes. Local sources are still fingerprint-validated so replacement bytes invalidate old playback tickets and caches.

## Play While Downloading

When a MediaSender ingestion is active, a processing movie or episode can become playable before the final file reaches local or Google Drive storage. StreamHome reads the submitted source once with FFmpeg and simultaneously creates:

- the lossless final media file used by the catalog; and
- an application-owned, growing H.264/AAC fMP4 HLS preview capped at 720p.

The preview publisher still waits for a safe buffer of at least 12 seconds and grows that target when ingestion is slower than real-time. The completed-media readiness policy prevents a partial preview from replacing the deterministic fully seekable catalog transport.

The browser never receives the submitted source URL or its authorization headers. It receives only renewable, playback-run-scoped StreamHome tickets and protected `/api/playback/preview/...` playlist and segment URLs.

When ingestion finishes, new playback runs use the completed catalog source. A preview run that is already playing continues through its completed application-owned preview cache, so the handoff does not reload the player or reveal a source change. Preview caches expire after 24 hours without use.

If preview transcoding is unsupported for a particular source, StreamHome records a preview error and retries normal ingestion without the preview branch. The media download can still complete even when early playback is unavailable. Cancelling a download or restarting the server ends the associated preview cleanly.

If playback fails, inspect the browser console, `backend.log`, FFmpeg availability, source metadata, and free space in the playback cache. Do not expose API port 8000 directly to clients.
