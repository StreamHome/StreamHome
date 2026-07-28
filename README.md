<div align="center">
  <img src="https://github.com/user-attachments/assets/4e188ccb-a419-444c-951c-391fc17c2a38" alt="StreamHome Media Server" width="100%">

  <br>

  <img src="https://readme-typing-svg.demolab.com?font=Oswald&weight=700&size=50&color=E25822&center=true&vCenter=true&width=500&height=80&lines=StreamHome&repeat=false" alt="StreamHome">

  <p>
    <b>A modern, open-source, self-hosted VOD platform built for seamless streaming, deep personalization, and minimal maintenance.</b>
  </p>

  <p>
    <a href="https://github.com/StreamHome/StreamHome/releases/tag/v0.1.0-alpha.1">
      <img src="https://img.shields.io/badge/Download-Early%20Public%20Alpha-E25822?style=for-the-badge&logo=github&logoColor=white" alt="Download Early Public Alpha">
    </a>
    <a href="https://github.com/StreamHome/StreamHome/releases">
      <img src="https://img.shields.io/github/v/release/StreamHome/StreamHome?include_prereleases&style=for-the-badge&label=Latest%20Release" alt="Latest Release">
    </a>
    <a href="LICENSE">
      <img src="https://img.shields.io/badge/License-GPLv3-blue?style=for-the-badge&logo=gnu&logoColor=white" alt="GPLv3 License">
    </a>
  </p>
</div>

---

## Your Personal Streaming Platform

StreamHome bridges the gap between premium VOD streaming experiences and the freedom of self-hosting.

Powered by a high-performance ASGI Python backend and a modern React 19 frontend, StreamHome transforms a personal media collection into a complete streaming platform with intelligent TMDB cataloging, adaptive HLS playback, personalized recommendations, cloud storage integration, automated recovery, and a source-agnostic ingestion API.

Your media remains under your control. StreamHome can store it locally, synchronize it with Google Drive, preserve portable recovery metadata alongside your media files, and maintain automatic local and cloud database backups.

> [!IMPORTANT]>
> The platform is still under active development.
>
> Back up your StreamHome data before installing an update.

## ✨ StreamHome at a Glance

<img width="1919" height="915" alt="StreamHome media details experience" src="https://github.com/user-attachments/assets/c2325c5d-d953-4082-8881-d9b3db9fec8b">

<p align="center">
  <i>A cinematic home experience personalized around the way you watch.</i>
</p>

<br>

<img width="1919" height="840" alt="StreamHome personalized home screen with cinematic hero section" src="https://github.com/user-attachments/assets/025339b7-668a-4159-843e-78d819c8ec1d">

<p align="center">
  <i>Rich metadata, personalized discovery, and playback controls in one unified experience.</i>
</p>

## Why StreamHome?

Most self-hosted media platforms begin after your media library has already been prepared.

StreamHome is designed to manage the complete VOD experience:

* ingesting media from compatible clients;
* processing video, audio, subtitles, and playback markers;
* retrieving localized metadata and artwork;
* organizing local and cloud storage;
* protecting and rebuilding catalog data;
* learning from profile-specific viewing behavior;
* recommending what to watch next;
* delivering secure and adaptive playback.

StreamHome is not intended to feel like a server administration panel. It is designed to feel like a complete streaming platform that happens to run entirely under your control.

## ✨ Core Features

### ☁️ Google Drive and Rclone Integration

Built-in Google Drive storage powered by Rclone and native Google OAuth.

StreamHome manages its own Rclone configuration and integrates cloud storage without disrupting the established local media directory structure.

### 🎨 Four Distinct Interface Themes

StreamHome includes four complete presentation systems:

* **Ember**
* **Aurora**
* **Cinema**
* **Gemini**

Each theme provides its own navigation, hero presentation, cards, details pages, visual language, and player styling rather than functioning as a simple color preset.

### 🧠 Personalized Recommendation Engine

A profile-scoped recommendation system uses viewing activity, watch time, explicit preferences, watchlist changes, selected search results, repeated viewing, early exits, exposure fatigue, availability, and catalog traits to generate personalized feeds.

Recommendations can also include clear reasons explaining why a title was selected.

### 🎬 Automated TMDB Cataloging

StreamHome automatically retrieves localized movie, series, and episode information, including:

* posters and backdrops;
* cast and crew;
* genres and production information;
* episode metadata;
* plot summaries;
* catalog and recovery records.

Artwork and portable recovery metadata are stored alongside the relevant media directories.

### 🌊 Adaptive HLS Streaming

A secure fMP4 HLS playback pipeline prepares media in four-second segments and generates a source-resolution-aware quality ladder from 1080p down to 240p when applicable.

Playback supports local and Google Drive media sources through the same unified experience.

### 🔌 Source-Agnostic Ingestion API

The extensible `/api/add-movie` endpoint accepts authorized HTTP media sources and supports:

* independent video and audio URLs;
* custom request headers;
* subtitles;
* language information;
* quality labels;
* movie, season, and episode information;
* intro, recap, credits, and preview markers.

Media can be submitted by any compatible MediaSender client.

Official browser integrations are available separately:

* [StreamHome Extension for Chrome](https://github.com/StreamHome/StreamHome-Extension-Chrome)
* [StreamHome Extension for Firefox](https://github.com/StreamHome/StreamHome-Extension-Firefox)

These integrations communicate with StreamHome through its public ingestion API and are not tightly coupled to the core platform.

### 🚀 Guided Web Setup

A protected `/setup` wizard guides users through:

* administrator account creation;
* TMDB configuration;
* ingestion credential generation;
* optional TOTP authentication;
* recovery-code generation;
* local storage selection;
* Google Drive authentication and folder selection;
* configuration validation.

StreamHome manages the underlying configuration without requiring users to manually configure Rclone through a terminal.

### 🔐 Authentication and Session Security

StreamHome includes:

* local account authentication;
* database-backed sessions;
* optional TOTP two-factor authentication;
* one-time recovery codes;
* session revocation;
* login protection and lockouts;
* recent reauthentication for sensitive actions;
* short-lived playback authorization tickets.

### 🗜️ Automated HEVC Optimization

An optional background optimization system converts supported media to HEVC/H.265 to reduce storage consumption.

Optimization is designed to run only while the system is idle and pause when active users require server resources, preventing background encoding from interfering with normal playback.

### 🛡️ Backup and Recovery Architecture

Portable metadata records stored alongside media files allow StreamHome to rebuild catalog information if the primary database is lost or corrupted.

The database can also be backed up automatically to:

* local storage;
* Google Drive.

The recovery architecture is designed to preserve media availability, catalog metadata, accounts, profiles, settings, and playback state.

### 📱 Responsive Web Client

StreamHome provides a responsive browser client for current desktop and mobile browsers. The alpha does not register a service worker and does not promise installable PWA behavior, offline playback, or offline downloads.

The server-delivered client stays current with the StreamHome version running on your server, without requiring separate native applications.

## ⚙️ How It Works

StreamHome handles ingestion, processing, cataloging, storage, cloud synchronization, recommendations, and playback preparation in the background.

### 1. 📥 Capture and Send

Select a video or audio source that you are authorized to access and use.

Send it to StreamHome through an official browser integration, a script, a CLI client, an automation tool, or any other compatible MediaSender implementation.

### 2. ⚡ Immediate Source Playback

When supported, StreamHome can begin playback from the submitted source while the server downloads and processes the media in the background.

### 3. 🔀 Seamless Source Handoff

After the locally managed media becomes available, StreamHome can transition playback from the incoming source while preserving the current playback position.

### 4. 🎬 Processing and Cataloging

FFmpeg and FFprobe inspect and process compatible video, audio, and subtitle tracks.

StreamHome then creates the final media structure, retrieves metadata and artwork, and updates its portable recovery records.

### 5. ☁️ Storage and Cloud Synchronization

Finalized media can remain in local storage or be synchronized with Google Drive through StreamHome’s application-managed Rclone configuration.

Rclone runs only on the StreamHome server; browser clients never receive its configuration or cloud credentials.

Cloud uploads are verified before local media is removed when that behavior is enabled.

### 6. 🍿 Personalized Playback

The completed title becomes available through the selected StreamHome theme with profile-specific:

* progress and resume state;
* watchlist state;
* personalized recommendations;
* audio tracks;
* subtitles;
* quality controls;
* playback speed;
* skip markers;
* completion tracking.

## 🖥️ Server Recommendations

StreamHome is designed to operate efficiently, but FFmpeg processing, adaptive playback preparation, cloud transfers, and optional HEVC optimization benefit from additional resources.

| Resource             | Minimum                                                          | Recommended                                                               |
| :------------------- | :--------------------------------------------------------------- | :------------------------------------------------------------------------ |
| **Operating System** | Ubuntu 22.04 or an equivalent current Linux distribution         | Ubuntu 24.04 or Debian 12                                                  |
| **CPU**              | 2 vCPUs                                                          | 4+ vCPUs                                                                  |
| **RAM**              | 2 GB                                                             | 4–6+ GB                                                                   |
| **Network**          | 20 Mbps (lower is no problem)                                    | Faster upload for multiple high-bitrate streams and cloud synchronization |
| **Local Storage**    | 20 GB for application data, metadata, cache, and temporary files | 50+ GB SSD, especially when using Google Drive media storage              |

Actual requirements depend on catalog size, source quality, concurrent playback sessions, cloud configuration, and active media-processing jobs.

Background HEVC optimization pauses when users require server resources and therefore should not normally reduce playback capacity.

## 📦 Installation

StreamHome currently supports Linux servers. Install it with the general installer:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The installer prepares the application in `~/StreamHome`, installs or verifies the required server dependencies, builds the production web client, and starts StreamHome. When both services are healthy, it prints the browser URL and the one-time bootstrap code used to unlock first-run setup.

Use a custom installation directory when needed:

```bash
STREAMHOME_INSTALL_DIR=/srv/streamhome \
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

To install and build without starting StreamHome:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh \
  | bash -s -- --no-start
```

After a no-start installation, run:

```bash
cd ~/StreamHome
./start.sh
```

See the [installation guide](docs/installation.md) for supported package managers, ports, lifecycle commands, and deployment notes.

## 📚 Documentation and Support

Complete documentation covering installation, setup, reverse proxies, Google Drive, MediaSender integration, updates, backup and recovery, troubleshooting, security, and API architecture is currently being prepared for the full public alpha release.

Until then:

* review the installation information included with the relevant release;
* report reproducible problems through [GitHub Issues](https://github.com/StreamHome/StreamHome/issues);
* report security vulnerabilities privately rather than through a public issue.

## 🚧 Alpha Status

StreamHome is actively preparing for its full public alpha release.

The early public alpha is intended for users who understand that:

* breaking changes may occur;
* installation procedures may evolve;
* migrations should always be preceded by a backup;
* platform and browser compatibility are still being expanded;
* TV remote navigation is not yet complete;
* some documentation is still being written.

A full public alpha should provide reliable installation, setup, ingestion, cataloging, playback, restart recovery, updates, and backup operations without requiring developer intervention.

## ⚠️ Responsible Use

Users are solely responsible for ensuring that they have the necessary rights and permissions for every media file, stream, URL, subtitle, and external source they ingest, process, store, or stream with StreamHome.

StreamHome:

* does not provide or distribute media;
* does not host a centralized content library;
* does not endorse third-party content sources;
* does not grant users rights to third-party content;
* does not support unauthorized access to protected services;
* does not support DRM circumvention or the bypassing of technological access controls.

StreamHome is a general-purpose, self-hosted media processing, storage, cataloging, recommendation, and playback platform.

It can be used with personal media, public-domain works, openly licensed content, and any other media the user is legally authorized to access and use.

## 📄 License

StreamHome is free and open-source software licensed under the **GNU General Public License v3.0**.

You may use, study, modify, and redistribute StreamHome under the terms of the GPLv3.

Modified versions distributed to others must remain licensed under the GPLv3 and include the corresponding source code as required by the license.

See the [`LICENSE`](LICENSE) file for the complete license text.

## 📫 Contact

Created and maintained by **waqsea**.

For questions, business inquiries, bug reports, or feature suggestions:

* **Email:** [streamhome@waqsea.com](mailto:streamhome@waqsea.com)
* **GitHub:** [@waqsea](https://github.com/WaqSea)

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
