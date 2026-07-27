# Playback

StreamHome serves local media from `/media` and prepares adaptive fMP4 HLS when direct playback is unsuitable.

Playback behavior includes:

- HTTP range requests for direct media;
- signed or authenticated playback preparation;
- adaptive video variants;
- multiple audio tracks and subtitles;
- profile-specific resume state;
- cleanup of expired playback runs and cache artifacts.

If playback fails, inspect the browser console, `backend.log`, FFmpeg availability, source metadata, and free space in the playback cache. Do not expose API port 8000 directly to clients.
