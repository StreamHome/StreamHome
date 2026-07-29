# Playback

StreamHome uses protected adaptive fMP4 HLS as its primary playback transport for completed local and Google Drive media.

Playback behavior includes:

- playback-run-scoped HLS manifests and segments;
- demand-prioritized playback preparation;
- adaptive video variants;
- multiple audio tracks and subtitles;
- profile-specific resume state;
- cleanup of expired playback runs and cache artifacts.

## Demand-first adaptive preparation

Opening a title gives its required video and default-audio renditions foreground priority. Lower-quality and secondary-audio renditions are generated afterward as preemptible background work. Server startup reconciles cache identities but does not queue full-catalog transcoding, so restarting or updating StreamHome cannot place the selected title behind every other item.

Compatible H.264 video and AAC audio are packaged into fMP4 HLS without re-encoding. Sources that are not browser-compatible receive a browser-compatible H.264/AAC baseline transcode. In both cases the protected master manifest is published when the required first segments exist; the complete file does not have to finish processing before playback begins.

The player reports whether preparation is queued, fast-packaging, or transcoding, along with queue position and generated segment count. FFmpeg jobs that stop producing output are terminated with a specific preparation failure instead of leaving an indefinite spinner. Prepared renditions remain cached and later quality changes reuse them.

## Play While Downloading

When a MediaSender ingestion is active, a processing movie or episode can become playable before the final file reaches local or Google Drive storage. StreamHome reads the submitted source once with FFmpeg and simultaneously creates:

- the lossless final media file used by the catalog; and
- an application-owned, growing H.264/AAC fMP4 HLS preview capped at 720p.

The player waits for a safe buffer before starting. The buffer target is at least 12 seconds and grows automatically when the observed ingestion speed is slower than real-time. This reduces the chance that playback catches the download on a slow connection.

The browser never receives the submitted source URL or its authorization headers. It receives only renewable, playback-run-scoped StreamHome tickets and protected `/api/playback/preview/...` playlist and segment URLs.

When ingestion finishes, new playback runs use the completed catalog source. A preview run that is already playing continues through its completed application-owned preview cache, so the handoff does not reload the player or reveal a source change. Preview caches expire after 24 hours without use.

If preview transcoding is unsupported for a particular source, StreamHome records a preview error and retries normal ingestion without the preview branch. The media download can still complete even when early playback is unavailable. Cancelling a download or restarting the server ends the associated preview cleanly.

If playback fails, inspect the browser console, `backend.log`, FFmpeg availability, source metadata, and free space in the playback cache. Do not expose API port 8000 directly to clients.
