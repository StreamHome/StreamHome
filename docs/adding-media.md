# Adding Media

This guide explains how to add movies and television episodes to StreamHome through a compatible MediaSender client.

StreamHome uses a source-agnostic ingestion system. Media is submitted through the authenticated `/api/add-movie` endpoint rather than being added directly through the web interface or copied manually into the database.

Compatible MediaSender clients may include:

* the official StreamHome browser extensions;
* custom MediaSender integrations;
* authorized automation tools;
* command-line clients;
* scripts implementing the MediaSender API.

> [!IMPORTANT]
> StreamHome does not provide media, media sources, or a built-in scraper.
>
> You are responsible for ensuring that you have permission to access, process, store, and stream every source submitted to your server.

## How Media Is Added

The standard media-ingestion process is:

1. Select a movie or television episode from an authorized source.
2. Open a compatible MediaSender client.
3. Connect the client to your StreamHome server.
4. Provide the StreamHome ingestion token.
5. Select the correct TMDB title.
6. Review the detected video, audio, subtitle, language, and quality information.
7. Send the media to StreamHome.
8. StreamHome validates the request and creates a background task.
9. The server downloads and processes the media.
10. TMDB metadata and artwork are retrieved.
11. The completed title becomes available in the StreamHome catalog.

When supported by the submitted source and installed StreamHome release, playback may begin from the incoming source while background processing continues.

## Before You Begin

You need:

* an installed and configured StreamHome server;
* an administrator-created ingestion token;
* a compatible MediaSender client;
* the address of your StreamHome server;
* an authorized HTTP or HTTPS media source;
* the correct TMDB identifier;
* enough local or cloud storage;
* FFmpeg and FFprobe available on the StreamHome server.

For remote MediaSender clients, StreamHome should be available through HTTPS.

Example server address:

```text
https://watch.example.com
```

## Official Browser Extensions

The official StreamHome browser extensions can act as MediaSender clients.

* [StreamHome Extension for Chrome](https://github.com/StreamHome/StreamHome-Extension-Chrome)
* [StreamHome Extension for Firefox](https://github.com/StreamHome/StreamHome-Extension-Firefox)

Install extensions only from the official StreamHome repositories or verified browser-store listings.

> [!WARNING]
> Do not install unofficial extensions that request your StreamHome ingestion token.
>
> A malicious extension possessing the token could submit unauthorized processing tasks to your server.

## Connect MediaSender to StreamHome

A MediaSender client normally requires two values:

* the StreamHome server address;
* the StreamHome ingestion token.

### Server Address

For a public installation, enter the root StreamHome address:

```text
https://watch.example.com
```

The client should send ingestion requests to:

```text
https://watch.example.com/api/add-movie
```

For local development, the internal API may be available at:

```text
http://localhost:8000/api/add-movie
```

Port `8000` should not normally be exposed directly to the internet.

### Ingestion Token

The ingestion token is generated during StreamHome's initial setup.

It is sent in this header:

```http
Authorization: Bearer <API_BEARER_TOKEN>
```

Treat this token like a password.

Never:

* publish it;
* include it in screenshots;
* commit it to Git;
* paste it into public issues;
* store it in a public extension bundle;
* send it to an unrelated service.

Regenerate the token if it becomes exposed.

## Add a Movie

To add a movie:

1. Open the authorized media source.
2. Open the MediaSender client.
3. Confirm that the media type is **Movie**.
4. Search for and select the correct TMDB movie.
5. Review the detected video source.
6. Review any separate audio source.
7. Review subtitles and language information.
8. Review the detected quality.
9. Review available playback markers.
10. Send the request to StreamHome.

A movie request must include:

* a valid TMDB movie identifier;
* `media_type` set to `movie`;
* a valid HTTP or HTTPS `video_url`.

Movie requests must not contain season or episode numbers.

Example movie information:

```json
{
  "tmdb_id": 550,
  "media_type": "movie",
  "video_url": "https://media.example.com/movies/example-movie.mp4",
  "quality": "1080p",
  "language": "en"
}
```

> [!IMPORTANT]
> Confirm that the selected TMDB title matches the submitted video.
>
> An incorrect TMDB identifier can cause StreamHome to save the wrong title, poster, backdrop, cast information, and description.

## Add a Television Episode

To add a television episode:

1. Open the authorized episode source.
2. Open the MediaSender client.
3. Confirm that the media type is **TV**.
4. Search for and select the correct TMDB series.
5. Select or confirm the season number.
6. Select or confirm the episode number.
7. Review the video and audio sources.
8. Review subtitles and language information.
9. Review the detected quality.
10. Review intro, recap, credits, or preview markers.
11. Send the request to StreamHome.

A television episode request must include:

* the TMDB identifier of the television series;
* `media_type` set to `tv`;
* a season number;
* an episode number;
* a valid HTTP or HTTPS `video_url`.

Example television episode information:

```json
{
  "tmdb_id": 1399,
  "media_type": "tv",
  "season": 1,
  "episode": 1,
  "video_url": "https://media.example.com/series/example-show/s01e01.m3u8",
  "quality": "1080p",
  "language": "en"
}
```

The `tmdb_id` identifies the series rather than an individual episode.

StreamHome uses the series identifier together with the season and episode numbers to retrieve the correct metadata.

## Video Sources

The `video_url` is the primary media source.

It must use HTTP or HTTPS.

Possible source types include:

* direct MP4 files;
* direct WebM files;
* HLS manifests;
* other formats supported by the installed FFmpeg version.

Direct file example:

```text
https://media.example.com/movies/movie.mp4
```

HLS example:

```text
https://media.example.com/movies/master.m3u8
```

Do not submit local filesystem paths directly:

```text
C:\Videos\movie.mp4
```

```text
/home/user/videos/movie.mp4
```

```text
file:///home/user/videos/movie.mp4
```

The public ingestion endpoint intentionally accepts HTTP and HTTPS sources only.

## Separate Audio Sources

Some sources provide video and audio separately.

A MediaSender client can submit an additional `audio_url`:

```json
{
  "video_url": "https://media.example.com/video/episode.m3u8",
  "audio_url": "https://media.example.com/audio/episode-en.m3u8"
}
```

When audio is already included in the video source, no separate audio URL is required.

StreamHome uses FFmpeg and FFprobe to inspect and process the submitted tracks.

## Request Headers

Some authorized sources require HTTP request headers.

Examples may include:

* `User-Agent`;
* `Referer`;
* `Origin`;
* `Cookie`.

A MediaSender client may submit these headers with the ingestion request.

Example:

```json
{
  "headers": {
    "User-Agent": "MediaSender/1.0",
    "Referer": "https://media.example.com/"
  }
}
```

Only submit headers required by the source.

> [!CAUTION]
> Cookies, signed URLs, authorization values, and source headers may contain private credentials.
>
> Do not share MediaSender logs publicly without removing sensitive values.

## Subtitles

A MediaSender client can submit external subtitles.

Each subtitle entry contains:

* a language code;
* an HTTP or HTTPS subtitle URL.

Example:

```json
{
  "subtitles": [
    {
      "language": "en",
      "url": "https://media.example.com/subtitles/movie-en.vtt"
    },
    {
      "language": "tr",
      "url": "https://media.example.com/subtitles/movie-tr.srt"
    }
  ]
}
```

The final supported formats depend on the installed StreamHome and FFmpeg versions.

StreamHome may normalize supported subtitle files for authenticated playback.

## Audio Language

The primary audio language may be submitted using a language code.

Examples:

```text
en
```

```text
tr
```

```text
de
```

The supplied language may override language information inferred from external metadata.

Confirm that the selected language matches the submitted audio.

## Source Quality

MediaSender may submit a quality label such as:

* `2160p`;
* `1080p`;
* `720p`;
* `480p`.

The quality value should describe the actual submitted source.

It should not claim a resolution higher than the source provides.

StreamHome may inspect the media independently and prepare an adaptive playback ladder based on the real source resolution.

## Playback Markers

MediaSender clients may submit known playback markers.

Supported marker categories are:

* intro;
* recap;
* credits;
* preview.

Each marker uses starting and ending positions in seconds.

Example:

```json
{
  "skip_markers": {
    "intro": [
      {
        "start": 60.0,
        "end": 145.0
      }
    ],
    "recap": [],
    "credits": [
      {
        "start": 3120.0,
        "end": 3240.0
      }
    ],
    "preview": []
  }
}
```

Marker requirements:

* values must use seconds;
* `start` must be earlier than `end`;
* values must not be negative;
* markers must correspond to the submitted media;
* unavailable marker types may use an empty array.

These markers may later appear as player actions such as skipping an intro or credits section.

## Review Before Sending

Before submitting a title, confirm:

* the correct StreamHome server is selected;
* the correct TMDB title is selected;
* the media type is correct;
* the video URL is present;
* the season and episode are correct for television media;
* movie requests contain no season or episode values;
* the selected language is correct;
* subtitle languages are correct;
* the quality label matches the source;
* required source headers are included;
* playback markers are valid;
* the ingestion token belongs to the target server.

Correcting information before submission avoids incorrect catalog records and unnecessary processing.

## Submit the Media

Use the MediaSender client's send or add action.

The exact button name depends on the client.

A successful request normally returns:

```http
201 Created
```

Example response:

```json
{
  "status": "success",
  "taskId": "7d9539bf-811c-4b5b-a621-e970b13dc00a",
  "title": "Example Movie",
  "message": "Media download task queued successfully."
}
```

A successful response means that StreamHome accepted and queued the request.

It does not mean that all processing has finished.

## What Happens After Submission

After a request is accepted, StreamHome may perform the following operations:

1. Validate the ingestion token.
2. Validate the request structure.
3. Create a background task.
4. Inspect the submitted source.
5. Begin supported immediate-source playback.
6. Download the video and audio.
7. Process compatible tracks with FFmpeg.
8. Download and prepare subtitles.
9. Retrieve TMDB metadata.
10. Download posters, backdrops, and other artwork.
11. Create or update recovery metadata.
12. Add the title to the database.
13. Prepare adaptive playback.
14. Upload the finalized media to Google Drive when enabled.
15. Verify cloud transfers before deleting an eligible local copy.

The exact operations depend on:

* media type;
* source format;
* audio configuration;
* subtitle configuration;
* storage mode;
* cloud configuration;
* installed StreamHome version.

## When Media Becomes Available

A queued title may pass through several states before becoming fully available.

Possible stages include:

* request validation;
* queued;
* downloading;
* processing;
* cataloging;
* playback preparation;
* cloud synchronization;
* completed;
* failed.

Do not repeatedly submit the same media while the first task is still processing.

When processing completes, the title should appear in the appropriate movie or series catalog.

## Movies and Series Storage

StreamHome organizes finalized media under its managed media structure.

Typical paths include:

```text
server/media/Movies
```

```text
server/media/Series
```

StreamHome may store the following alongside the media:

* metadata;
* posters;
* backdrops;
* episode artwork;
* subtitles;
* audio tracks;
* playback markers;
* recovery information.

Do not rename or move files while an ingestion task is active.

## Adding Local Files

The public `/api/add-movie` endpoint does not accept filesystem paths.

For development and diagnostics, StreamHome includes:

```text
server/scratch/test_ingest_stream.py
```

When a local file is selected, the diagnostic:

1. creates a temporary local HTTP server;
2. exposes the file through a loopback address;
3. submits the temporary HTTP URL;
4. keeps the bridge running until processing ends;
5. verifies the resulting catalog record.

Windows example:

```powershell
venv\Scripts\python.exe server\scratch\test_ingest_stream.py --video "C:\path\to\video.mp4"
```

This is a development and testing utility rather than the normal end-user MediaSender workflow.

## Adding Multiple Episodes

Submit each television episode as a separate ingestion request.

Each episode must contain its own:

* season number;
* episode number;
* video source;
* optional audio source;
* subtitles;
* quality;
* language;
* playback markers.

Do not submit multiple episode numbers inside one standard MediaSender request unless a future StreamHome release explicitly introduces batch ingestion.

A client may automate multiple individual requests, but each request must still follow the MediaSender API contract.

## Re-Adding Existing Media

Before adding a title again, check whether it already exists in the catalog or processing queue.

Submitting the same movie or episode multiple times may result in:

* duplicate tasks;
* repeated downloads;
* additional storage usage;
* overwritten or conflicting files;
* repeated metadata processing.

The exact duplicate-handling behavior may vary by StreamHome release.

Do not rely on automatic duplicate detection as a replacement for reviewing the target title before submission.

## Removing an Active Source

A temporary source URL, cookie, or signed request may expire before StreamHome finishes downloading it.

Keep the authorized source available until the task has completed.

Do not:

* close a local source bridge;
* revoke required access;
* remove the source file;
* invalidate required cookies;
* allow a short-lived signed URL to expire;

while StreamHome is still processing the task.

## Common Problems

### MediaSender cannot connect to StreamHome

Check:

* the server address is correct;
* StreamHome is running;
* HTTPS is valid;
* the reverse proxy is working;
* the firewall permits access;
* the client is using `/api/add-movie`;
* the internal API port is not being used incorrectly.

### Request returns `401 Unauthorized`

The ingestion token is missing or invalid.

Confirm that the request contains:

```http
Authorization: Bearer <API_BEARER_TOKEN>
```

Also confirm that:

* the complete token was entered;
* the token belongs to the selected server;
* the token has not been regenerated;
* there are no additional spaces or quotation marks.

### Request returns `422 Unprocessable Entity`

The request contains missing or invalid fields.

Check:

* `tmdb_id` is an integer;
* `media_type` is `movie` or `tv`;
* `video_url` is present;
* television requests contain `season` and `episode`;
* movie requests omit `season` and `episode`;
* subtitle objects contain `language` and `url`;
* playback-marker values are numeric.

### Wrong title appears

The submitted TMDB identifier was probably incorrect.

Confirm:

* the title;
* release year;
* media type;
* series;
* season;
* episode.

Delete or correct the affected record only through supported StreamHome administration tools.

### Source cannot be downloaded

Check:

* the URL is still active;
* the server can reach the source;
* required headers were included;
* cookies have not expired;
* signed URL parameters remain valid;
* redirects lead to an accessible media source;
* the source is a supported media file or manifest.

### Direct MP4 fails

Confirm that the URL points directly to the media file.

Do not add HLS-specific options or rewrite a direct file URL as a manifest.

StreamHome classifies and configures FFmpeg input options on the server.

### HLS source fails

Check:

* the manifest URL is valid;
* child playlists and segments are reachable;
* required headers apply to the manifest and segment requests;
* the source has not expired;
* the server can resolve every referenced URL.

### Subtitles are missing

Check:

* the subtitle URL is valid;
* the subtitle language was provided;
* the server can download the subtitle;
* the format is supported;
* source headers are also supplied when required.

### Media remains queued

The background worker may be processing another task.

Also check:

* available disk space;
* FFmpeg availability;
* server memory;
* active cloud transfers;
* existing background jobs;
* server diagnostics.

### Media is processed but unavailable

StreamHome may have completed downloading but failed during:

* catalog finalization;
* database update;
* playback preparation;
* cloud verification;
* metadata retrieval.

Review the task's final status and server diagnostics.

## Security Checklist

Before using MediaSender, verify:

* [ ] The client comes from a trusted source.
* [ ] The StreamHome server address is correct.
* [ ] HTTPS is enabled for remote access.
* [ ] The ingestion token is stored privately.
* [ ] The token is not hardcoded into public files.
* [ ] Source cookies and headers are not logged publicly.
* [ ] The TMDB title matches the source.
* [ ] The server has sufficient storage.
* [ ] FFmpeg and FFprobe are working.
* [ ] Google Drive is healthy when cloud storage is enabled.
* [ ] You have permission to use the submitted media.

## Responsible Use

Users are responsible for ensuring that they possess the necessary rights and permissions for every source submitted to StreamHome.

StreamHome:

* does not provide or distribute media;
* does not include a centralized media library;
* does not grant access to third-party content;
* does not bypass DRM;
* does not bypass technological access controls;
* does not authorize access to protected services;
* does not endorse unauthorized sources.

Use StreamHome with:

* personal media;
* public-domain works;
* openly licensed content;
* media you created;
* media you are otherwise legally authorized to access and use.

## Related Documentation

* [Getting Started](getting-started.md)
* [Installation](installation.md)
* [Initial Setup](setup.md)
* [MediaSender Integration](mediasender.md)
* [Google Drive Integration](google-drive.md)
* [Playback](playback.md)
* [Security](security.md)
* [Troubleshooting](troubleshooting.md)

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
