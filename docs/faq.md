# StreamHome Frequently Asked Questions

This page answers common questions about installing, configuring, using, updating, and troubleshooting StreamHome.

> [!IMPORTANT]
> StreamHome is currently alpha software.
>
> Features, APIs, supported platforms, installation procedures, and configuration formats may change between releases. Always read the release notes and create a backup before updating.

## General

### What is StreamHome?

StreamHome is an open-source, self-hosted Video on Demand platform.

It combines:

* personalized media discovery;
* movie, series, season, and episode cataloging;
* source-agnostic media ingestion;
* adaptive HLS playback;
* local and Google Drive storage;
* automatic backup and recovery;
* multiple user profiles;
* four distinct interface themes;
* Progressive Web App support.

StreamHome is designed to provide a premium streaming experience while keeping the server, media, profiles, and catalog data under the user's control.

### Is StreamHome a media server?

Yes, but StreamHome is designed to provide more than basic media file playback.

It manages the full VOD workflow:

1. Media submission
2. Downloading and processing
3. Metadata and artwork retrieval
4. Catalog creation
5. Local or cloud storage
6. Personalized recommendations
7. Secure playback
8. Progress and completion tracking
9. Backup and recovery

### Is StreamHome open source?

Yes.

StreamHome is licensed under the **GNU General Public License v3.0**.

See the [`LICENSE`](../LICENSE) file for the complete license terms.

### Is StreamHome free?

Yes.

StreamHome itself does not require a subscription.

Some optional external services, storage providers, domains, servers, or infrastructure used with StreamHome may have their own costs.

### Is StreamHome ready for production use?

StreamHome is currently in alpha.

Core workflows are designed to work reliably, but alpha releases may still contain:

* compatibility problems;
* incomplete documentation;
* breaking changes;
* migration changes;
* device-specific issues;
* unfinished features.

Users should maintain current backups and review the release notes before every update.

### Who is StreamHome intended for?

StreamHome is intended for users who want:

* a self-hosted VOD experience;
* control over their media and data;
* personalized recommendations;
* local or Google Drive storage;
* a premium web interface;
* flexible media-ingestion options;
* an open-source alternative to closed streaming platforms.

## Installation

### How do I install StreamHome?

Open the [StreamHome Releases](https://github.com/StreamHome/StreamHome/releases) page and follow the installation instructions included with the exact release you want to install.

Do not use installation scripts from the mutable development branch unless the release documentation explicitly instructs you to do so.

See [Installation](installation.md) for more information.

### Which operating systems are supported?

Supported operating systems are listed in each release.

Only environments explicitly listed in the relevant release notes should be considered officially tested.

Other operating systems may work, but compatibility is not guaranteed during alpha development.

### Can I install StreamHome on Windows?

Yes, when the selected release provides and supports a Windows installer.

Run PowerShell with the permissions required by that release's installation instructions.

### Can I install StreamHome on Linux?

Yes.

Supported Linux distributions are listed in the release documentation. Ubuntu and Debian are the primary Linux targets.

### Is macOS supported?

macOS should only be considered supported when it is explicitly listed in the release notes.

An application working in an untested environment does not mean that the platform is officially supported.

### Can I install StreamHome on a VPS?

Yes.

The server must provide sufficient:

* CPU;
* RAM;
* storage;
* network bandwidth;
* permissions;
* supported operating-system compatibility.

For public internet access, HTTPS and a properly configured reverse proxy are strongly recommended.

### Can I install StreamHome without a desktop computer?

It may be possible to manage a remote server from a phone using an SSH application or similar tool.

However, the exact process depends on the server provider and operating system. A computer is not strictly required if the full installation and setup process can be completed from another device.

### Does StreamHome require Docker?

No, unless a specific release explicitly uses or provides a Docker installation method.

Follow the installation method documented for the release you are installing.

### Where are the installation commands?

Installation commands are provided in the relevant GitHub Release.

This prevents users from accidentally installing a mutable development version when they intended to install a stable alpha tag.

## Initial Setup

### What happens after installation?

Open the URL shown by the installer.

A fresh installation should direct you to:

`/setup`

The setup wizard guides you through the initial configuration.

See [Initial Setup](setup.md).

### What does the setup wizard configure?

Depending on the release and selected options, setup may configure:

* the administrator account;
* administrator password;
* TOTP two-factor authentication;
* recovery codes;
* TMDB credentials;
* MediaSender credentials;
* local storage;
* Google Drive;
* Rclone;
* backup settings;
* application validation.

### Why does `/setup` redirect me to `/login`?

This is normal after setup has been completed.

StreamHome prevents the initial setup process from being run again through the normal public setup route.

### Why can I not reopen the setup wizard?

The setup wizard is intended for initial installation.

After setup has been completed, configuration should be managed through the relevant administrative interfaces.

Recovery or setup-reset procedures should only be used when documented for the installed release.

### What happens if setup is interrupted?

Restart StreamHome and open the server URL again.

The setup system should either:

* continue from a safe incomplete state;
* request the missing configuration;
* return an actionable error.

Do not manually edit the database unless the official documentation instructs you to do so.

### Do I need a domain?

No, a domain is not required for local-network use.

A domain is strongly recommended when exposing StreamHome over the public internet because it simplifies:

* HTTPS;
* reverse-proxy configuration;
* trusted cookies;
* OAuth callback URLs;
* external access.

## Accounts and Profiles

### What is the difference between an account and a profile?

An account handles authentication and server access.

A profile stores personalized viewing data such as:

* watch progress;
* history;
* watchlist;
* likes, loves, and dislikes;
* recommendations;
* completed titles;
* repeated viewing;
* Continue Watching;
* Watch Again.

Multiple profiles can use the same authenticated environment while keeping viewing preferences separate.

### Should every viewer have an administrator account?

No.

Administrator access is only possible through the administrator profile initially created by the system, which cannot be deleted.

### Does StreamHome support two-factor authentication?

Yes.

StreamHome supports TOTP-based two-factor authentication when enabled.

Store recovery codes securely and outside the StreamHome server directory.

### What happens if I lose my authenticator?

Use one of the recovery codes generated when TOTP was enabled.

Each recovery code should be treated as sensitive authentication material.

### Can I revoke active sessions?

Yes.

StreamHome supports session revocation and administrative session management.

The exact interface may vary by release.

## Media and Catalog

### Does StreamHome include movies or series?

No.

StreamHome does not provide, sell, distribute, or centrally host media.

Users are responsible for adding media they are authorized to access and use.

### Where does StreamHome obtain posters and metadata?

StreamHome uses TMDB for catalog metadata and artwork.

This may include:

* titles;
* descriptions;
* posters;
* backdrops;
* cast and crew;
* genres;
* seasons;
* episodes;
* production information.

TMDB is not used as a media source.

### Can StreamHome catalog movies and series?

Yes.

StreamHome supports:

* movies;
* television series;
* seasons;
* episodes.

### What is a metadata-only title?

A metadata-only title exists in the catalog for discovery or recommendation purposes but does not currently have playable media attached to it.

A playable title has an available local, incoming, or cloud media source.

### Can search results overwrite playable media?

They should not.

Metadata caching and playable media availability are treated as separate states so catalog enrichment does not replace or destroy playable records.

### Where is metadata stored?

StreamHome stores catalog information in its database.

Portable recovery metadata and artwork may also be stored alongside media files inside their relevant directories.

This allows catalog records to be reconstructed if required.

## Adding Media

### How do I add media?

Media is submitted through the StreamHome ingestion API using a compatible MediaSender client.

Possible clients include:

* the official Chrome extension;
* the official Firefox extension;
* scripts;
* command-line tools;
* automation systems;
* independently developed integrations.

See [Adding Media](adding-media.md).

### Is the browser extension required?

No.

The browser extensions are separate integrations built on top of the public ingestion API.

StreamHome does not depend on a specific extension or client.

### Can someone build their own StreamHome client?

Yes.

A compatible application can submit valid MediaSender requests using the documented API and properly scoped credentials.

### What information can an ingestion request contain?

Depending on the request and client, it may contain:

* TMDB ID;
* media type;
* video URL;
* separate audio URL;
* season and episode information;
* subtitles;
* languages;
* quality labels;
* permitted request headers;
* intro markers;
* recap markers;
* preview markers;
* credit markers.

### Should movie requests include season or episode numbers?

No.

Season and episode fields are intended for episodic content.

### What happens after media is submitted?

The request enters the background-processing queue.

StreamHome may then:

1. Validate the request
2. Inspect the source
3. Download video and audio
4. Process or merge media through FFmpeg
5. Handle subtitles
6. Retrieve TMDB metadata
7. Download artwork
8. Create catalog records
9. Update recovery metadata
10. Upload finalized media to Google Drive when enabled

### Can StreamHome begin playback before downloading finishes?

When the submitted source and current release support it, StreamHome can begin playback from the incoming source while processing continues in the background.

After local media becomes available, playback may transition to the managed copy while preserving the current position.

### Why did my ingestion job fail?

Common causes include:

* invalid credentials;
* inaccessible source URLs;
* expired signed URLs;
* invalid request headers;
* unavailable video or audio;
* incorrect TMDB ID;
* insufficient storage;
* FFmpeg failure;
* network interruption;
* Google Drive failure;
* unsupported media structure.

Check the structured queue error before retrying.

### Are failed jobs retried automatically?

Transient failures may be retried.

Permanent failures, invalid submissions, authorization errors, or unavailable sources may require user action.

### Does StreamHome deduplicate ingestion jobs?

StreamHome is designed to prevent duplicate work where possible.

The exact duplicate-detection behavior may depend on the request, media identity, and release.

## Playback

### Which playback format does StreamHome use?

StreamHome uses an adaptive fMP4 HLS playback pipeline.

Media may be prepared in short segments and served through a source-resolution-aware quality ladder.

### Does StreamHome support seeking?

Yes.

Seeking is supported for compatible local and Google Drive playback sources.

### Does StreamHome support multiple qualities?

Yes.

When applicable, StreamHome can provide quality options from the source resolution down to lower prepared resolutions.

The exact ladder depends on the source and current configuration.

### Does StreamHome support subtitles?

Yes.

Supported subtitle availability depends on the submitted media and browser.

### Does StreamHome support multiple audio tracks?

Yes.

Available audio tracks can be selected from the player when the processed media contains them.

### Does StreamHome remember playback progress?

Yes.

Progress and completion state are stored separately for each profile.

### Can I restart a movie from the beginning?

Yes.

The player can provide a Start Over action for titles with stored progress.

### Does StreamHome support intro and credit skipping?

Yes, when playback markers are available.

Supported marker types may include:

* intro;
* recap;
* preview;
* credits.

### Does StreamHome support Picture-in-Picture?

Picture-in-Picture may be available on compatible browsers and devices.

### Does StreamHome work on phones?

Yes.

StreamHome includes responsive playback interfaces and specialized landscape controls for supported mobile browsers.

### Does StreamHome support TV remotes?

Full TV-remote navigation is still being developed.

Many TV remotes send keyboard-style directional events, so improved keyboard navigation will also improve remote compatibility.

### Why does HEVC playback not work on my device?

HEVC support depends on:

* browser;
* operating system;
* device hardware;
* installed codecs;
* media container.

StreamHome's background HEVC optimization reduces storage usage, but not every browser can decode HEVC directly.

Compatibility behavior may vary by release.

### Does HEVC optimization reduce playback capacity?

Normally, no.

HEVC optimization is designed to run while the system is idle and pause when active users require server resources.

Real-time transcoding has different performance requirements and should not be confused with idle background optimization.

See [Playback](playback.md).

## Recommendations

### How does the recommendation system work?

Recommendations are profile-specific.

The engine may use:

* viewing history;
* watch time;
* completion;
* early exits;
* repeated viewing;
* likes, loves, and dislikes;
* watchlist activity;
* selected search results;
* genres;
* collections;
* cast and crew;
* exposure fatigue;
* available catalog media;
* exploration and discovery signals.

### Does StreamHome use the same recommendations for every profile?

No.

Each profile develops its own preferences, history, and recommendation state.

### Why are recommendations limited on a new profile?

A new profile has little or no viewing history.

The system may begin with selected genres, catalog traits, exploration, and available media until more profile-specific behavior is collected.

### Why is a particular movie recommended?

StreamHome can provide recommendation reasons depending on the recommendation source and available catalog data.

### What is Watch Again?

Watch Again is a recency-based collection of previously completed or watched titles.

It is intentionally separated from the main recommendation-ranking system.

## Themes and Interface

### How many themes does StreamHome include?

StreamHome includes four interface themes:

* Ember
* Aurora
* Cinema
* Gemini

### Are the themes only different colors?

No.

The themes can use different:

* navigation structures;
* hero sections;
* card styles;
* details layouts;
* visual geometry;
* player presentation;
* interaction language.

### Which theme is the default?

Ember is the primary default experience unless changed by the user or release configuration.

## Google Drive

### Is Google Drive required?

No.

StreamHome can operate with local storage only.

### What does Google Drive integration provide?

Depending on configuration, Google Drive can be used for:

* finalized media storage;
* media synchronization;
* database backups;
* cloud playback;
* recovery operations.

### Does StreamHome configure Rclone automatically?

StreamHome includes a guided web-based Google Drive and Rclone setup process.

Users should not normally need to configure Rclone manually through the terminal.

### What OAuth client type should I use?

For a StreamHome web callback using a public domain, create a Google OAuth client with the **Web application** type.

A Desktop OAuth client does not provide the same registered web callback configuration.

### What does `redirect_uri_mismatch` mean?

It means the callback URL sent by StreamHome does not exactly match an authorized redirect URI configured for the OAuth client.

For example, if StreamHome sends:

`https://watch.example.com/api/setup/rclone/drive/callback`

the exact same value must be added to the OAuth client's authorized redirect URIs.

The following differences matter:

* `http` versus `https`;
* domain;
* subdomain;
* port;
* path;
* trailing slash.

### Should I use my server IP or domain for Google OAuth?

For a publicly accessible web installation, use the HTTPS domain that StreamHome actually uses.

Use the callback value shown in the Google error details rather than guessing the path.

### Should the Google OAuth application remain in Testing mode?

Testing mode is suitable while only listed test users are connecting.

For a public release intended for arbitrary users, the OAuth application may need to be moved to production and may require additional Google verification depending on the requested scopes.

### Why does Google Drive setup fail even though login succeeds?

Possible causes include:

* incorrect OAuth Client ID;
* incorrect OAuth secret;
* mismatched callback URI;
* missing test-user access;
* insufficient Drive permissions;
* incorrect folder selection;
* Rclone configuration failure;
* blocked cookies;
* reverse-proxy issues;
* server time errors.

See [Google Drive Integration](google-drive.md).

## Storage, Backup, and Recovery

### Where is the StreamHome database stored?

The primary application database is stored in the server data directory defined by the installation.

All application components should use the same active database location.

### Does StreamHome create backups automatically?

StreamHome can maintain automatic local and Google Drive database backups when configured.

### What does a backup contain?

A database backup may preserve:

* accounts;
* profiles;
* catalog records;
* sessions;
* settings;
* playback progress;
* recommendations;
* configuration stored in the database.

Media files and external configuration files may require separate backup handling depending on the deployment.

### Can StreamHome recover from database corruption?

StreamHome stores portable metadata alongside media directories.

This allows the catalog to be reconstructed from available media and recovery records if the primary database is lost or corrupted.

### Does recovery recreate everything?

Catalog recovery can rebuild media-related records from portable metadata.

Accounts, secrets, sessions, profile history, and other database-only information still depend on valid database backups.

### Should I test restoring a backup?

Yes.

A backup should not be considered reliable until its creation and restoration procedure have been tested.

See [Backup and Recovery](backup-and-recovery.md).

## PWA and Offline Use

### Can StreamHome be installed like an application?

Yes.

On supported browsers, StreamHome can be installed as a Progressive Web App.

The option may appear as:

* Install app;
* Add to Home Screen;
* Create shortcut;
* Install StreamHome.

### Does StreamHome have native mobile applications?

StreamHome primarily uses its responsive PWA experience instead of maintaining separate native applications for every mobile and desktop platform.

### Does the PWA update automatically?

The interface is delivered by the StreamHome server, so users normally receive the client version associated with their installed server release.

Browser caching behavior may temporarily retain an older interface until the application refreshes.

### Does the PWA support offline downloads?

StreamHome provides supported offline-download functionality.

Availability and behavior may depend on:

* browser;
* operating system;
* storage permissions;
* media format;
* selected StreamHome release.

### Can I add StreamHome to a television home screen?

Some smart TV browsers allow websites or PWAs to be added to the home screen.

Support depends entirely on the TV operating system and browser.

See [Progressive Web App](pwa.md).

## Updates

### How do I update StreamHome?

Follow the update instructions included with the release you are installing.

Do not replace files manually unless the release documentation explicitly instructs you to do so.

### Should I back up before updating?

Yes.

Create and verify a current backup before every alpha update.

### Will updates preserve my media and database?

Updates are designed to preserve application data, media, configuration, accounts, and profiles.

However, alpha releases may introduce migrations or breaking changes. Always review the release notes.

### Can I downgrade?

Downgrading may not be safe after a database or configuration migration.

Only use a documented rollback process and restore a compatible backup when required.

### Why should installers use a release tag instead of `main`?

A release tag is immutable and corresponds to a documented version.

The `main` branch can change at any time and may include incomplete development work.

See [Updating StreamHome](updating.md).

## Security and Privacy

### Does StreamHome send my media or catalog to a central StreamHome server?

No.

StreamHome is self-hosted.

Your server, media, database, and profiles remain under your control unless you configure an external storage or network service.

### Does StreamHome collect telemetry?

Telemetry behavior should be explicitly described by the installer or website.

Any optional installation event or anonymous usage statistic should be transparent and should not include media, credentials, server secrets, or personal catalog information.

### Is StreamHome secure when exposed to the internet?

No internet-facing application is secure without proper deployment.

Use:

* HTTPS;
* a trusted reverse proxy;
* strong passwords;
* two-factor authentication;
* limited administrative access;
* firewall rules;
* current updates;
* secure credential storage;
* session revocation when required.

### Can I share my administrator password?

No.

Anyone with administrator credentials may gain access to sensitive server operations and data.

### Can I share my MediaSender credential?

Only with trusted clients that require ingestion access.

Integration credentials should be scoped, revocable, and kept separate from administrator sessions.

### Where should security vulnerabilities be reported?

Do not report security vulnerabilities through public GitHub Issues.

Follow the private reporting instructions in [`SECURITY.md`](../SECURITY.md).

### What information should never be posted publicly?

Never publish:

* passwords;
* session tokens;
* ingestion credentials;
* API keys;
* OAuth secrets;
* Google refresh tokens;
* TOTP secrets;
* recovery codes;
* cookies;
* signed URLs;
* private domains;
* internal IP addresses;
* personal media information;
* unsanitized logs.

## Responsible Use

### Is StreamHome a content provider?

No.

StreamHome does not provide, sell, distribute, or endorse third-party media.

### Can StreamHome be used with public-domain or openly licensed media?

Yes.

StreamHome can be used with:

* personal media;
* public-domain works;
* openly licensed content;
* authorized private sources;
* any media the user has the necessary rights to access and use.

### Who is responsible for media submitted to StreamHome?

The user operating the server is responsible for ensuring that they have the necessary rights and permissions for all submitted media, URLs, subtitles, and sources.

### Does StreamHome support DRM circumvention?

No.

StreamHome does not support bypassing DRM or technological access controls.

## Performance

### How many users can watch at the same time?

Concurrent-user capacity depends on:

* CPU;
* RAM;
* upload bandwidth;
* storage speed;
* media bitrate;
* cloud performance;
* whether media requires real-time processing;
* active background jobs.

A server with 4 CPU cores and 6 GB RAM may serve several simultaneous prepared streams, but network bandwidth is often the main limit when no real-time transcoding is required.

Actual capacity should be measured on the target server.

### Does Google Drive reduce playback performance?

Google Drive playback depends on:

* Drive API performance;
* server network connectivity;
* cache state;
* media bitrate;
* concurrent access;
* provider limits.

StreamHome can use local caching and unified playback resolution to improve cloud playback behavior.

### Does background HEVC conversion slow down viewers?

It should not during normal operation.

The HEVC optimization worker is designed to pause when active users require server resources.

## Troubleshooting

### StreamHome does not start. What should I check?

Check:

* installer output;
* supported operating system;
* available disk space;
* required dependencies;
* active ports;
* service logs;
* database health;
* configuration completeness.

### Why am I seeing an old interface after updating?

The browser or installed PWA may still be using cached assets.

Try:

1. Refreshing the page
2. Closing and reopening the PWA
3. Clearing the site's cached data
4. Restarting the current production service

Do not delete application data unless instructed.

### Why does the page work on one device but not another?

Possible causes include:

* browser compatibility;
* cached assets;
* codec support;
* network access;
* HTTPS certificate problems;
* reverse-proxy configuration;
* device-specific PWA behavior.

### Why do posters or backdrops appear blank?

Possible causes include:

* unavailable TMDB artwork;
* stale artwork cache;
* missing local files;
* failed metadata download;
* network connectivity problems;
* an invalid cached artwork path.

Check server logs and metadata cache status.

### Why is a catalog title visible but not playable?

It may be a metadata-only suggestion or the media may currently be unavailable.

Confirm that ingestion completed and that a valid local, incoming, or Google Drive source exists.

### Why are personalized recommendations missing?

Recommendations may be limited when:

* the profile is new;
* there is insufficient viewing activity;
* catalog enrichment is incomplete;
* recommendation backfill is still running;
* few playable titles are available.

### Why does the queue show a failure?

Open the job details and review the structured error.

Do not repeatedly retry a permanently invalid request without correcting the underlying problem.

### Where can I get more help?

Use the following order:

1. [Getting Started](getting-started.md)
2. [Installation](installation.md)
3. [Initial Setup](setup.md)
4. [Adding Media](adding-media.md)
5. [Playback](playback.md)
6. [Troubleshooting](troubleshooting.md)
7. Existing [GitHub Issues](https://github.com/StreamHome/StreamHome/issues)

When opening an issue, include:

* StreamHome version;
* operating system;
* browser and device;
* installation method;
* storage mode;
* expected behavior;
* actual behavior;
* exact reproduction steps;
* sanitized logs.

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
