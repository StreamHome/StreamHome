# Playback

StreamHome serves local media from `/media` and prepares adaptive fMP4 HLS when direct playback is unsuitable.

Playback behavior includes:

- HTTP range requests for direct media;
- signed or authenticated playback preparation;
- adaptive video variants;
- multiple audio tracks and subtitles;
- profile-specific resume state;
- cleanup of expired playback runs and cache artifacts.

## Play While Downloading

When a MediaSender ingestion is active, a processing movie or episode can become playable before the final file reaches local or Google Drive storage. StreamHome reads the submitted source once with FFmpeg and simultaneously creates:

- the lossless final media file used by the catalog; and
- an application-owned, growing H.264/AAC fMP4 HLS preview capped at 720p.

The player waits for a safe buffer before starting. The buffer target is at least 12 seconds and grows automatically when the observed ingestion speed is slower than real-time. This reduces the chance that playback catches the download on a slow connection.

The browser never receives the submitted source URL or its authorization headers. It receives only renewable, playback-run-scoped StreamHome tickets and protected `/api/playback/preview/...` playlist and segment URLs.

When ingestion finishes, new playback runs use the completed catalog source. A preview run that is already playing continues through its completed application-owned preview cache, so the handoff does not reload the player or reveal a source change. Preview caches expire after 24 hours without use.

If preview transcoding is unsupported for a particular source, StreamHome records a preview error and retries normal ingestion without the preview branch. The media download can still complete even when early playback is unavailable. Cancelling a download or restarting the server ends the associated preview cleanly.

If playback fails, inspect the browser console, `backend.log`, FFmpeg availability, source metadata, and free space in the playback cache. Do not expose API port 8000 directly to clients.
