# StreamHome Documentation

Welcome to the official documentation for **StreamHome**, a modern, open-source, self-hosted VOD platform focused on personalized discovery, flexible media ingestion, adaptive playback, cloud storage, and complete user control.

StreamHome is designed to provide a premium streaming experience without requiring users to give up ownership of their media, catalog data, profiles, or server infrastructure.

> [!IMPORTANT]
> StreamHome is currently in active alpha development.
>
> Features, APIs, installation procedures, supported platforms, and configuration formats may change between alpha releases. Always create a backup before updating.

## Documentation Overview

### Getting Started

New to StreamHome? Start here.

* [Getting Started](getting-started.md)
* [Installation](installation.md)
* [Frequently Asked Questions](faq.md)

The Getting Started guide covers the complete path from installation to playing your first title.

### Setup and Configuration

Learn how to configure StreamHome through the protected web setup wizard.

* [Initial Setup](setup.md)
* [Google Drive Integration](google-drive.md)
* [Reverse Proxy and HTTPS](reverse-proxy.md)
* [Progressive Web App](pwa.md)

The setup wizard handles administrator creation, TMDB configuration, ingestion credentials, optional two-factor authentication, storage configuration, and Google Drive connectivity.

### Adding Media

StreamHome uses a source-agnostic ingestion system.

* [Adding Media](adding-media.md)
* [MediaSender API](mediasender.md)
* [Chrome Extension](https://github.com/StreamHome/StreamHome-Extension-Chrome)
* [Firefox Extension](https://github.com/StreamHome/StreamHome-Extension-Firefox)

Media may be submitted through the official browser extensions, compatible third-party clients, scripts, CLI tools, or automation systems using the MediaSender API.

### Playback

Learn how StreamHome prepares and delivers media.

* [Playback Overview](playback.md)
* [Audio and Subtitles](audio-and-subtitles.md)
* [Playback Progress and Resume](progress-and-resume.md)
* [Quality Selection and HLS](adaptive-streaming.md)

StreamHome supports secure adaptive fMP4 HLS playback, multiple audio tracks, subtitles, quality selection, resume state, playback markers, and profile-specific progress tracking.

### Recommendations and Profiles

StreamHome provides personalized recommendations for each profile.

* [Profiles](profiles.md)
* [Recommendation System](recommendations.md)
* [Watchlist and Reactions](watchlist-and-reactions.md)

Recommendations may use viewing history, watch time, explicit preferences, watchlist activity, search interactions, repeated viewing, early exits, catalog traits, and recommendation exposure.

### Storage, Backup, and Recovery

Learn how StreamHome protects media and application data.

* [Storage Overview](storage.md)
* [Google Drive Storage](google-drive.md)
* [Backup and Restore](backup-and-recovery.md)
* [Catalog Recovery](catalog-recovery.md)
* [HEVC Optimization](hevc-optimization.md)

StreamHome can store media locally, synchronize finalized media with Google Drive, maintain local and cloud database backups, and rebuild catalog records from portable metadata stored alongside media files.

### Administration

Manage and maintain a StreamHome installation.

* [Admin Center](admin-center.md)
* [Updating StreamHome](updating.md)
* [Logs and Diagnostics](diagnostics.md)
* [Troubleshooting](troubleshooting.md)
* [Uninstalling StreamHome](uninstalling.md)

### Security

* [Security Overview](security.md)
* [Authentication and Sessions](authentication.md)
* [Two-Factor Authentication](two-factor-authentication.md)
* [Deployment Security](deployment-security.md)
* [Reporting a Vulnerability](../SECURITY.md)

Security vulnerabilities should not be reported through public GitHub issues.

### Developer Documentation

Documentation for contributors and integration developers.

* [Architecture Overview](development/architecture.md)
* [Development Setup](development/setup.md)
* [API Reference](development/api.md)
* [MediaSender Integration](development/mediasender-integration.md)
* [Testing](development/testing.md)
* [Contributing](../CONTRIBUTING.md)

## Recommended Reading Order

For a new installation, follow this order:

1. [Installation](installation.md)
2. [Initial Setup](setup.md)
3. [Getting Started](getting-started.md)
4. [Adding Media](adding-media.md)
5. [Playback Overview](playback.md)
6. [Backup and Restore](backup-and-recovery.md)
7. [Updating StreamHome](updating.md)
8. [Troubleshooting](troubleshooting.md)

## Supported Platforms

Officially supported platforms will be listed in the installation documentation for each release.

Only platforms and browsers explicitly listed for a release should be considered tested. Other environments may work, but compatibility is not guaranteed during the alpha period.

## Getting Help

Before opening an issue:

1. Confirm that you are using the latest available StreamHome release.
2. Review the relevant documentation page.
3. Check the [FAQ](faq.md).
4. Review the [Troubleshooting Guide](troubleshooting.md).
5. Search existing [GitHub Issues](https://github.com/StreamHome/StreamHome/issues).

When reporting a problem, include:

* StreamHome version
* operating system
* browser and device
* installation method
* storage mode
* relevant reproduction steps
* sanitized logs
* expected behavior
* actual behavior

Never include passwords, API credentials, cookies, session tokens, OAuth secrets, signed URLs, private domains, or personal media information in public reports.

## Responsible Use

Users are responsible for ensuring that they have the necessary rights and permissions for all media, URLs, subtitles, and external sources submitted to StreamHome.

StreamHome does not provide media, distribute media, endorse third-party sources, support unauthorized access, or support DRM circumvention.

For additional information, see the Responsible Use section in the main [README](../README.md).

## Project Links

* [Main Repository](https://github.com/StreamHome/StreamHome)
* [Releases](https://github.com/StreamHome/StreamHome/releases)
* [Issues](https://github.com/StreamHome/StreamHome/issues)
* [Chrome Extension](https://github.com/StreamHome/StreamHome-Extension-Chrome)
* [Firefox Extension](https://github.com/StreamHome/StreamHome-Extension-Firefox)
* [License](../LICENSE)

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
