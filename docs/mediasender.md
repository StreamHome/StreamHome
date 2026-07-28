# MediaSender Integration

MediaSender is StreamHome's source-agnostic media ingestion interface.

A compatible MediaSender client collects the information required to process a media source and submits it directly to the StreamHome server through the `/api/add-movie` endpoint.

MediaSender clients may include:

* the official StreamHome browser extensions;
* custom browser extensions;
* authorized automation tools;
* command-line scripts;
* server integrations;
* other applications implementing the MediaSender API contract.

> [!IMPORTANT]
> StreamHome does not include a built-in scraper or provide media sources.
>
> MediaSender only transfers source information supplied by an authorized client. Users are responsible for ensuring that they have permission to access, process, store, and stream every submitted source.

## How MediaSender Works

The standard ingestion process is:

1. A compatible client identifies an authorized media source.
2. The client collects the TMDB identifier and media type.
3. The client prepares the video, audio, subtitle, language, header, quality, and playback-marker information.
4. The client sends the payload to StreamHome.
5. StreamHome validates the request and creates an ingestion task.
6. The background queue processes the submitted source.
7. FFmpeg and FFprobe inspect and prepare the media.
8. StreamHome retrieves metadata and artwork.
9. The completed title is added to the StreamHome catalog.

MediaSender does not need direct access to StreamHome's database or media directories.

All ingestion must occur through the authenticated API.

## Authentication

Every MediaSender request must include the StreamHome ingestion token in the HTTP `Authorization` header.

Header format:

```http
Authorization: Bearer <API_BEARER_TOKEN>
```

The first ingestion key is generated during StreamHome's initial setup. To create additional keys, open **Admin → API Keys**, enter a descriptive name, and select **Add media**. Keys are independent: creating a new one does not revoke existing keys.

The complete key is shown only once. StreamHome stores a secure hash plus a non-secret hint, so a lost key must be replaced. Each key can be renamed, assigned different permissions, given an optional expiration, or revoked individually from the same page.

> [!WARNING]
> Treat the ingestion token like a password.
>
> Anyone possessing a valid key with the **Add media** permission can submit media-processing tasks to the server.

Never:

* commit the token to Git;
* hardcode it in a public extension;
* include it in screenshots;
* paste it into public issues;
* store it in public JavaScript bundles;
* print it in application logs;
* send it to third-party services.

A MediaSender client should store the token using the most secure storage mechanism available on its platform.

## Ingestion Endpoint

### Public or Reverse-Proxy Installation

Use the public StreamHome address:

```text
https://YOUR_STREAMHOME_DOMAIN/api/add-movie
```

Example:

```text
https://watch.example.com/api/add-movie
```

### Local Development

When communicating directly with the internal FastAPI server during local development:

```text
http://localhost:8000/api/add-movie
```

> [!IMPORTANT]
> Port `8000` is normally an internal application port.
>
> A public MediaSender client should usually communicate through the StreamHome domain and reverse proxy rather than exposing the internal FastAPI port directly.

### Request Details

| Property           | Value              |
| ------------------ | ------------------ |
| **Method**         | `POST`             |
| **Content type**   | `application/json` |
| **Authentication** | Bearer Token       |
| **Endpoint**       | `/api/add-movie`   |

## Request Schema

| Field                | Type             | Required | Description                                                                        |
| -------------------- | ---------------- | :------: | ---------------------------------------------------------------------------------- |
| `tmdb_id`            | Integer          |    Yes   | TMDB identifier for the movie or television series.                                |
| `media_type`         | String           |    Yes   | Media type. Use `"movie"` for movies or `"tv"` for television series.              |
| `video_url`          | String           |    Yes   | Direct HTTP or HTTPS video file or stream-manifest URL, such as MP4, WebM, or HLS. |
| `audio_url`          | String or `null` |    No    | Separate audio source when the video and audio are provided independently.         |
| `video_source_type`  | String           |    No    | `"auto"` (default) or `"hls"` when the client positively identifies a manifest.    |
| `audio_source_type`  | String           |    No    | `"auto"` (default) or `"hls"` for a separately supplied audio manifest.            |
| `season`             | Integer          |  TV only | Season number for a television episode.                                            |
| `episode`            | Integer          |  TV only | Episode number for a television episode.                                           |
| `headers`            | Object           |    No    | Request headers required to access the submitted source.                           |
| `subtitles`          | Array            |    No    | External subtitle sources with language and URL information.                       |
| `quality`            | String           |    No    | Source-quality label such as `"1080p"` or `"720p"`.                                |
| `language`           | String           |    No    | Primary audio-language code such as `"en"` or `"tr"`.                              |
| `skip_markers`       | Object           |    No    | Intro, recap, credits, and preview playback markers.                               |

## Field Requirements

### `tmdb_id`

The `tmdb_id` must identify the title represented by the submitted media source.

Movie example:

```json
{
  "tmdb_id": 550
}
```

Television-series example:

```json
{
  "tmdb_id": 1399
}
```

The client must not submit an unrelated TMDB identifier.

Incorrect identifiers may cause StreamHome to retrieve the wrong:

* title;
* poster;
* backdrop;
* cast;
* season information;
* episode information;
* localized metadata.

### `media_type`

Supported values are:

```text
movie
```

```text
tv
```

Use `movie` for a standalone movie.

Use `tv` for an episode belonging to a television series.

Clients should normalize alternative internal labels before sending the request.

### `video_url`

The `video_url` must be an HTTP or HTTPS source.

Supported source categories may include:

* direct MP4 files;
* direct WebM files;
* HLS manifests;
* other source formats supported by the installed StreamHome and FFmpeg versions.

Example:

```json
{
  "video_url": "https://media.example.com/video/movie.mp4"
}
```

Local filesystem paths must not be sent directly:

```text
C:\Videos\movie.mp4
```

```text
/home/user/videos/movie.mp4
```

```text
file:///home/user/videos/movie.mp4
```

The ingestion endpoint intentionally accepts network sources rather than arbitrary server filesystem paths.

### `audio_url`

Use `audio_url` when the video and audio are available as separate sources.

Example:

```json
{
  "video_url": "https://media.example.com/video/episode.m3u8",
  "audio_url": "https://media.example.com/audio/episode-en.m3u8"
}
```

When the submitted video already includes the required audio, omit `audio_url` or use:

```json
{
  "audio_url": null
}
```

### `season` and `episode`

These fields are required for television episodes.

Example:

```json
{
  "media_type": "tv",
  "season": 1,
  "episode": 4
}
```

Movie requests must omit `season` and `episode` entirely.

Do not submit them as:

```json
{
  "season": null,
  "episode": null
}
```

Recommended movie payloads contain neither field.

### `headers`

Some authorized sources require request headers.

Example:

```json
{
  "headers": {
    "User-Agent": "MediaSender/1.0",
    "Referer": "https://media.example.com/"
  }
}
```

Possible headers include:

* `User-Agent`;
* `Referer`;
* `Origin`;
* `Cookie`;
* other source-specific HTTP headers.

> [!CAUTION]
> Headers may contain authentication cookies or other private information.
>
> A MediaSender client must not log, publish, synchronize, or expose sensitive header values unnecessarily.

Only include headers required by the submitted source.

### `subtitles`

Subtitles are submitted as an array.

Each subtitle object contains:

| Field      | Type   | Required | Description                 |
| ---------- | ------ | :------: | --------------------------- |
| `language` | String |    Yes   | Subtitle-language code.     |
| `url`      | String |    Yes   | HTTP or HTTPS subtitle URL. |

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

Use an empty array when the client explicitly provides no subtitles:

```json
{
  "subtitles": []
}
```

The final supported subtitle formats depend on the installed StreamHome and FFmpeg versions.

### `quality`

The `quality` field describes the submitted source.

Examples:

```text
2160p
```

```text
1080p
```

```text
720p
```

```text
480p
```

This value is a descriptive source label. It should not claim a resolution higher than the submitted media.

### `language`

The `language` field identifies the primary audio language.

Use a normalized language code when known:

```json
{
  "language": "en"
}
```

```json
{
  "language": "tr"
}
```

This value may override language information inferred from external metadata.

### `skip_markers`

Skip markers allow a MediaSender client to submit known playback segments.

Supported categories are:

* `intro`;
* `recap`;
* `credits`;
* `preview`.

Each marker contains a starting and ending position in seconds.

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

Marker rules:

* `start` and `end` use seconds;
* `start` must be earlier than `end`;
* values should not be negative;
* markers should correspond to the submitted media;
* missing categories may be omitted;
* empty categories may use an empty array.

## Movie Payload Example

```json
{
  "tmdb_id": 550,
  "media_type": "movie",
  "video_url": "https://media.example.com/movies/example-movie.mp4",
  "audio_url": null,
  "headers": {
    "User-Agent": "MediaSender/1.0",
    "Referer": "https://media.example.com/"
  },
  "subtitles": [
    {
      "language": "en",
      "url": "https://media.example.com/subtitles/example-movie-en.vtt"
    },
    {
      "language": "tr",
      "url": "https://media.example.com/subtitles/example-movie-tr.vtt"
    }
  ],
  "quality": "1080p",
  "language": "en",
  "skip_markers": {
    "credits": [
      {
        "start": 8100.0,
        "end": 8300.0
      }
    ]
  }
}
```

Notice that the movie payload does not contain `season` or `episode`.

## Television Episode Payload Example

```json
{
  "tmdb_id": 1399,
  "media_type": "tv",
  "season": 1,
  "episode": 1,
  "video_url": "https://media.example.com/series/example-show/s01e01-video.m3u8",
  "audio_url": "https://media.example.com/series/example-show/s01e01-audio-en.m3u8",
  "headers": {
    "User-Agent": "MediaSender/1.0",
    "Referer": "https://media.example.com/"
  },
  "subtitles": [
    {
      "language": "en",
      "url": "https://media.example.com/subtitles/example-show-s01e01-en.vtt"
    }
  ],
  "quality": "1080p",
  "language": "en",
  "skip_markers": {
    "intro": [
      {
        "start": 120.0,
        "end": 210.0
      }
    ],
    "recap": [
      {
        "start": 0.0,
        "end": 115.0
      }
    ],
    "credits": [
      {
        "start": 3400.0,
        "end": 3600.0
      }
    ],
    "preview": []
  }
}
```

## Browser Extension Example

The following example demonstrates the request structure used by a browser-extension MediaSender client.

```javascript
const streamHomeBaseUrl = "https://watch.example.com";
const apiBearerToken = "YOUR_PRIVATE_INGESTION_TOKEN";

const payload = {
  tmdb_id: 550,
  media_type: "movie",
  video_url: "https://media.example.com/movies/example-movie.mp4",
  headers: {
    "User-Agent": navigator.userAgent,
    "Referer": window.location.href
  },
  quality: "1080p",
  language: "en",
  skip_markers: {
    intro: [],
    credits: []
  }
};

const response = await fetch(`${streamHomeBaseUrl}/api/add-movie`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${apiBearerToken}`
  },
  body: JSON.stringify(payload)
});

const result = await response.json();

if (!response.ok) {
  throw new Error(result.detail || `Request failed with ${response.status}`);
}

console.log("Ingestion task created:", result);
```

> [!WARNING]
> This example uses a visible variable only to demonstrate the request structure.
>
> A distributed browser extension must not contain a shared StreamHome token in its source code. The token should be supplied and stored separately for each user's own server.

## cURL Example

```bash
curl -X POST "https://watch.example.com/api/add-movie" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -d '{
    "tmdb_id": 550,
    "media_type": "movie",
    "video_url": "https://media.example.com/movies/example-movie.mp4",
    "quality": "1080p",
    "language": "en",
    "skip_markers": {
      "intro": [],
      "credits": []
    }
  }'
```

Store the token in an environment variable rather than typing it directly into shell history when possible.

Linux example:

```bash
export API_BEARER_TOKEN="YOUR_PRIVATE_INGESTION_TOKEN"
```

PowerShell example:

```powershell
$env:API_BEARER_TOKEN = "YOUR_PRIVATE_INGESTION_TOKEN"
```

## Python Example

```python
from __future__ import annotations

import os

import requests


streamhome_base_url = "https://watch.example.com"
api_bearer_token = os.environ.get("API_BEARER_TOKEN")

if not api_bearer_token:
    raise RuntimeError("API_BEARER_TOKEN is not configured.")

payload = {
    "tmdb_id": 550,
    "media_type": "movie",
    "video_url": "https://media.example.com/movies/example-movie.mp4",
    "quality": "1080p",
    "language": "en",
    "skip_markers": {
        "intro": [],
        "credits": [],
    },
}

response = requests.post(
    f"{streamhome_base_url}/api/add-movie",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_bearer_token}",
    },
    json=payload,
    timeout=30,
)

response.raise_for_status()
print(response.json())
```

## Successful Response

A successful ingestion request returns:

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

The `taskId` identifies the background ingestion task.

A successful response means the request was accepted and queued. It does not necessarily mean that downloading, processing, cataloging, or cloud synchronization has already finished.

## Error Responses

### Unauthorized

Returned when the Bearer Token is missing or invalid:

```http
401 Unauthorized
```

Example:

```json
{
  "detail": "Invalid or missing API Bearer token."
}
```

Check:

* the `Authorization` header exists;
* the header begins with `Bearer`;
* the complete token was provided;
* the token belongs to the target StreamHome server;
* the token has not been regenerated or revoked;
* the client is not sending additional quotation marks.

### Validation Error

Returned when required fields are missing or use an invalid type:

```http
422 Unprocessable Entity
```

Common causes include:

* missing `tmdb_id`;
* missing `media_type`;
* missing `video_url`;
* a string supplied where an integer is required;
* missing `season` or `episode` for a television request;
* malformed subtitle objects;
* malformed skip-marker values.

Review the response body for field-specific validation information.

## Local Ingestion Smoke Test

StreamHome includes an interactive local ingestion diagnostic:

```text
server/scratch/test_ingest_stream.py
```

The diagnostic can accept:

* a direct HTTP or HTTPS media URL;
* a local media-file path for testing.

For a local file, the diagnostic does not send the filesystem path to `/api/add-movie`.

Instead, it:

1. starts a temporary loopback HTTP server;
2. provides HEAD and byte-range support;
3. exposes the local file through a temporary `127.0.0.1` URL;
4. submits that HTTP URL to the ingestion endpoint;
5. keeps the temporary bridge active while the queue processes the task;
6. waits until the task reaches a final state;
7. verifies the resulting catalog record.

This preserves StreamHome's HTTP and HTTPS-only ingestion boundary.

### Linux Development Example

From the StreamHome project directory:

```bash
venv/bin/python server/scratch/test_ingest_stream.py --video "/path/to/video.mp4"
```

The diagnostic uses StreamHome's existing environment configuration.

It may also:

* search for TMDB titles using configured credentials;
* read supported TheIntroDB markers;
* convert marker values from milliseconds to seconds;
* omit `season` and `episode` for movie requests;
* monitor the ingestion task in `server/database.db`;
* verify the final TMDB metadata;
* verify skip markers;
* verify the final playable catalog URL.

The diagnostic must not print or store:

* the ingestion token;
* TMDB credentials;
* TheIntroDB credentials;
* authentication cookies;
* private request headers.

## Direct Files and HLS Sources

Direct media files and HLS manifests use the same MediaSender request structure, but they may require different FFmpeg input handling.

Direct file example:

```text
https://media.example.com/movie.mp4
```

HLS example:

```text
https://media.example.com/movie/master.m3u8
```

StreamHome applies HLS-specific FFmpeg options only when the source is identified as an HLS manifest.

Identification does not depend only on a `.m3u8` suffix. If a provider returns an HLS playlist from a disguised URL such as `master.txt`, submit the extension's positive classification:

```json
{
  "video_url": "https://media.example.com/hls/movie.mp4/txt/master.txt",
  "video_source_type": "hls",
  "headers": {
    "Referer": "https://media.example.com/",
    "User-Agent": "Mozilla/5.0"
  }
}
```

For older clients that leave the default `"auto"`, StreamHome inspects FFprobe's detected container and performs one forced-HLS retry when an HTTP source is ambiguous. Once identified, the queue persists the HLS type so the same header and demuxer contract is used during download and for nested segment requests.

A MediaSender client should not attempt to provide FFmpeg command-line options directly.

The server remains responsible for:

* source classification;
* FFprobe inspection;
* FFmpeg input configuration;
* media processing;
* path validation;
* final cataloging.

## Security Recommendations

### Use HTTPS

Remote MediaSender clients should communicate with StreamHome through HTTPS.

Without HTTPS, the following information may be exposed in transit:

* the ingestion token;
* source URLs;
* request headers;
* cookies;
* subtitle URLs;
* media metadata.

### Restrict the Token

Use a separate StreamHome ingestion token rather than an administrator password.

Regenerate the token when:

* it is accidentally published;
* a device containing it is lost;
* an extension installation is no longer trusted;
* suspicious ingestion tasks appear;
* a third-party client no longer requires access.

### Validate the Target Server

Before sending private source information, confirm that:

* the domain belongs to your StreamHome installation;
* HTTPS is valid;
* the endpoint path is `/api/add-movie`;
* the server certificate is trusted;
* no unexpected redirect sends the request elsewhere.

### Avoid Public Logs

MediaSender clients should redact:

* Bearer Tokens;
* cookies;
* authorization headers;
* signed URLs;
* temporary source parameters;
* personal filesystem paths;
* private domain names when sharing diagnostics publicly.

## Responsible Use

MediaSender is a general-purpose ingestion interface.

Users are responsible for ensuring that they have the necessary rights and permissions for every:

* media source;
* stream;
* URL;
* audio track;
* subtitle;
* request header;
* playback marker;
* file submitted to StreamHome.

StreamHome and MediaSender:

* do not provide media;
* do not host a centralized content catalog;
* do not grant access rights to third-party sources;
* do not bypass DRM;
* do not bypass technological access controls;
* do not authorize access to protected services;
* do not replace the user's responsibility to comply with applicable laws and service terms.

## Troubleshooting

### Request returns `401 Unauthorized`

Verify the Bearer Token and header format:

```http
Authorization: Bearer <API_BEARER_TOKEN>
```

Do not use:

```http
Authorization: <API_BEARER_TOKEN>
```

StreamHome returns `missing_integration_credential` when the Bearer header is absent and `invalid_integration_credential` when the key is invalid, expired, or revoked.

### Request returns `403 Forbidden`

Read the JSON response body before changing the key:

* `insufficient_scope` means the key does not currently have **Add media** permission;
* `cross_site_request_blocked` means a cookie-authenticated mutation failed the browser-origin boundary;
* a non-JSON Cloudflare or reverse-proxy response means the request was rejected before StreamHome handled it.

Extension requests that provide an explicit `shk_` Bearer key are authenticated as machine requests even when the browser also has a StreamHome login cookie. The API key is still validated independently, and an invalid key cannot fall back to the cookie session.

### Request returns `422 Unprocessable Entity`

Confirm that:

* `tmdb_id` is an integer;
* `media_type` is `movie` or `tv`;
* `video_url` is present;
* television payloads contain `season` and `episode`;
* movie payloads omit `season` and `episode`;
* subtitle entries contain `language` and `url`;
* skip markers contain numeric `start` and `end` values.

### Browser reports a CORS error

Confirm that the MediaSender client is using a supported integration method and the correct StreamHome domain.

Do not expose the internal API port publicly merely to avoid a browser security restriction.

### Submitted source cannot be downloaded

Check:

* the URL is still valid;
* the server can reach the source;
* required headers were included;
* cookies or signed parameters have not expired;
* the source supports the request method StreamHome requires;
* redirects do not lead to a blocked or inaccessible address;
* the source is an actual media file or supported manifest.

### Direct MP4 fails while HLS works

Confirm that the submitted URL is recognized as a direct media file rather than an HLS manifest.

The client should submit the original source URL without attempting to attach HLS-specific FFmpeg options.

### Local filesystem path is rejected

This is expected.

The public ingestion endpoint accepts HTTP and HTTPS sources.

Use the included local ingestion smoke-test utility when testing a local file.

### Wrong movie or episode metadata appears

Verify:

* `tmdb_id`;
* `media_type`;
* `season`;
* `episode`.

The TMDB identifier must represent the submitted title.

### Ingestion request succeeds but media is not immediately available

A `201 Created` response confirms that the task was queued.

StreamHome may still need to:

* download the source;
* inspect tracks;
* process video or audio;
* download subtitles;
* fetch TMDB metadata;
* create artwork and recovery files;
* update the database;
* prepare playback;
* synchronize cloud storage.

Review the task state and server diagnostics for the final result.

## Related Documentation

* [Getting Started](getting-started.md)
* [Installation](installation.md)
* [Initial Setup](setup.md)
* [Adding Media](adding-media.md)
* [Google Drive Integration](google-drive.md)
* [Playback](playback.md)
* [Security](security.md)
* [Troubleshooting](troubleshooting.md)

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
