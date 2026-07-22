<div align="center">
  <img src="https://github.com/user-attachments/assets/4e188ccb-a419-444c-951c-391fc17c2a38" alt="StreamHome Media Server" width="100%">

  <br>

  <img src="https://readme-typing-svg.demolab.com?font=Oswald&weight=700&size=50&color=E25822&center=true&vCenter=true&width=500&height=80&lines=StreamHome&repeat=false" alt="StreamHome">

  <p>
    <b>A modern, self-hosted VOD platform built for seamless streaming, deep personalization, and minimal maintenance.</b>
  </p>
</div>

---

StreamHome bridges the gap between premium VOD streaming experiences and the freedom of self-hosting.

Built with a high-performance ASGI Python backend and a modern React 19 frontend, StreamHome brings personal media libraries to life through intelligent TMDB cataloging, adaptive HLS playback, personalized recommendations, cloud storage integration, and a source-agnostic ingestion API.

Your media remains under your control. StreamHome can store media locally, synchronize it with Google Drive, preserve portable metadata alongside media files, and automatically maintain local and cloud database backups.

> [!IMPORTANT]
> StreamHome is currently under active development and has not yet reached its first public alpha release. Features, APIs, installation steps, and system requirements may change before release.

## ✨ Core Features

1. ☁️ **Google Drive and Rclone Integration**
   Built-in Google Drive storage powered by Rclone and native Google OAuth. StreamHome manages its own Rclone configuration and integrates cloud storage without altering the established local media directory structure.

2. 🎨 **Multi-Theme UI Matrix**
   A premium React 19 interface with four distinct presentation systems: Ember, Aurora, Cinema, and Gemini. Each theme provides its own navigation, cards, details pages, visual language, and player styling.

3. 🧠 **Personalized Recommendation Engine**
   A profile-scoped recommendation system that uses viewing activity, watch time, explicit preferences, watchlist changes, selected search results, repeated viewing, exposure fatigue, availability, and catalog traits to generate personalized feeds and recommendation reasons.

4. 🎬 **Automated TMDB Cataloging**
   Automatically retrieves localized movie, series, and episode metadata, including posters, backdrops, cast information, production details, and plot summaries. Artwork and recovery metadata are stored alongside the relevant media directories.

5. 🌊 **Adaptive HLS Streaming**
   A secure fMP4 HLS playback pipeline that prepares media in four-second segments and provides a source-resolution-aware quality ladder from 1080p down to 240p when applicable.

6. 🔌 **Source-Agnostic Ingestion API**
   The extensible `/api/add-movie` endpoint accepts authorized HTTP media sources, including separate video and audio URLs, custom request headers, subtitles, language information, quality labels, and playback markers.

   Media can be submitted through any compatible MediaSender client, including the official:

   * [StreamHome Extension for Chrome](https://github.com/StreamHome/StreamHome-Extension-Chrome)
   * [StreamHome Extension for Firefox](https://github.com/StreamHome/StreamHome-Extension-Firefox)

7. 🚀 **Guided Web Setup and Security**
   A protected `/setup` wizard handles administrator creation, TMDB configuration, ingestion-token generation, optional TOTP authentication, recovery codes, storage selection, and Google Drive connection without requiring manual Rclone configuration.

8. 🗜️ **Automated HEVC Optimization**
   An optional background optimization system can convert supported media to HEVC/H.265 to reduce storage usage. Optimization is designed to run during idle periods and pause when active users require server resources.

9. 🛡️ **Failsafe Recovery Architecture**
   Localized metadata records stored alongside media files allow StreamHome to rebuild catalog records if the primary database is lost or corrupted. The database can also be backed up automatically to local storage and Google Drive.

10. 🔐 **Authenticated Playback Sessions**
    Playback uses database-backed runs and renewable, short-lived tickets scoped to the authenticated session, profile, media item, playback run, and source fingerprint.

11. 📱 **Installable Progressive Web App**
    StreamHome can be installed as a PWA on supported desktop and mobile browsers, providing an application-like experience while remaining automatically synchronized with the server-delivered web client.

## ⚙️ How It Works

StreamHome handles complex ingestion, media processing, cataloging, cloud synchronization, and playback preparation in the background.

1. 📥 **Capture and Send**
   Select a video or audio source that you are authorized to access and use. Send it to StreamHome through an official browser extension or another compatible MediaSender client.

2. ⚡ **Immediate Source Playback**
   When supported, StreamHome can begin playback from the submitted source while the server processes and downloads the media in the background.

3. 🔀 **Seamless Source Handoff**
   After the local media becomes available, StreamHome can transition playback from the incoming source to the locally managed version while preserving the current playback position.

4. 🎬 **Processing and Cataloging**
   FFmpeg and FFprobe inspect the source, process compatible video, audio, and subtitle tracks, and create the final media structure.

5. ☁️ **Metadata and Cloud Synchronization**
   StreamHome retrieves TMDB metadata, downloads artwork, updates recovery records, and optionally uploads the finalized media to Google Drive through its application-managed Rclone configuration.

6. 🍿 **Personalized Playback Experience**
   The completed title becomes available through the selected StreamHome theme, with profile-specific progress, watchlist state, recommendations, subtitles, audio tracks, quality controls, and playback markers.

## 🖥️ Server Recommendations

StreamHome is designed to operate efficiently, but real-time FFmpeg processing, adaptive playback preparation, cloud transfers, and optional HEVC optimization benefit from additional resources.

| Resource             | Minimum                                                   | Recommended                                                  |
| :------------------- | :-------------------------------------------------------- | :----------------------------------------------------------- |
| **Operating System** | Ubuntu 22.04 or Windows 10                                | Ubuntu 24.04 or Debian 12                                    |
| **CPU**              | 2 vCPUs                                                   | 4+ vCPUs                                                     |
| **RAM**              | 2 GB                                                      | 4+ GB                                                        |
| **Network**          | 20 Mbps                                                   | 1 Gbps for high-bitrate streaming and cloud synchronization  |
| **Local Storage**    | 20 GB for application data, metadata, and temporary files | 50+ GB SSD, especially when using Google Drive media storage |

Actual requirements depend on catalog size, source quality, concurrent playback sessions, transcoding activity, and cloud configuration.

## 📦 Quick Installation

The bootstrap installers clone StreamHome into `~/StreamHome`, install missing supported dependencies, build the production web client, and start the protected web setup wizard.

> [!CAUTION]
> A command piped into a shell executes the downloaded script immediately. Use the inspect-first alternatives below if you want to review it before execution.

### Linux and macOS

```bash
curl -fsSL https://raw.githubusercontent.com/WaqSea/StreamHome/main/install.sh | bash
```

Inspect first:

```bash
curl -fsSLo streamhome-install.sh https://raw.githubusercontent.com/WaqSea/StreamHome/main/install.sh
less streamhome-install.sh
bash streamhome-install.sh
```

The automated system-package step supports APT-based Linux distributions, Fedora/RHEL, Arch Linux, and macOS with Homebrew. Python 3.11+, Node.js 18+, FFmpeg/FFprobe, rclone, Git, and npm are required.

### Windows — PowerShell

```powershell
irm https://raw.githubusercontent.com/WaqSea/StreamHome/main/install.ps1 | iex
```

Inspect first:

```powershell
irm https://raw.githubusercontent.com/WaqSea/StreamHome/main/install.ps1 -OutFile streamhome-install.ps1
Get-Content .\streamhome-install.ps1
.\streamhome-install.ps1
```

Windows uses Winget for missing Python, Node.js, FFmpeg, rclone, or Git installations. If Winget installs a dependency but Windows has not refreshed the command path yet, open a new PowerShell window and run the installer again.

### Installation options

Use environment variables to select another directory or Git branch/tag:

```bash
STREAMHOME_INSTALL_DIR=/opt/streamhome STREAMHOME_REF=main \
  bash streamhome-install.sh
```

```powershell
$env:STREAMHOME_INSTALL_DIR = "D:\StreamHome"
$env:STREAMHOME_REF = "main"
.\streamhome-install.ps1
```

An existing installation is updated only when it points to the official StreamHome repository and has no local changes. Updates are fast-forward-only. Unrelated, non-empty, or dirty directories are never overwritten.

To install and build without starting the services:

```bash
./setup.sh --no-start
```

```powershell
.\setup.bat --no-start
```

After setup, open `http://localhost:3000/setup`. The terminal displays the one-time bootstrap code. Change `WEB_PORT` in the root `.env` before starting if port 3000 is unavailable. Existing `.env`, database, media, backup, and Google Drive/rclone state are preserved when setup is run again.

## 📚 Documentation and Support

Detailed documentation for installation, reverse proxies, MediaSender integration, cloud storage, security, recovery, troubleshooting, and API architecture will be published alongside the public alpha release.

👉 **Documentation is not yet available.**

## ⚠️ Responsible Use

Users are solely responsible for ensuring that they have the necessary rights and permissions for every media file, stream, URL, subtitle, and external source they ingest, process, store, or stream with StreamHome.

StreamHome:

* does not provide or distribute media;
* does not host a centralized content library;
* does not endorse third-party content sources;
* does not grant users rights to third-party content;
* does not support unauthorized access to protected services;
* does not support DRM circumvention or the bypassing of technological access controls.

StreamHome is a general-purpose, self-hosted media processing, storage, cataloging, recommendation, and playback platform. It can be used with personal media, public-domain works, openly licensed content, and any other media the user is legally authorized to access and use.

## 📄 License

StreamHome is free and open-source software licensed under the **GNU General Public License v3.0**.

You may use, study, modify, and redistribute StreamHome under the terms of the GPLv3. Modified versions distributed to others must remain licensed under the GPLv3 and include the corresponding source code as required by the license.

See the [`LICENSE`](LICENSE) file for the complete license text.

## 📫 Contact

Created and maintained by **waqsea**.

For questions, business inquiries, bug reports, or feature suggestions:

* **Email:** [StreamHome@waqsea.com](mailto:StreamHome@waqsea.com)
* **GitHub:** [@waqsea](https://github.com/WaqSea)
