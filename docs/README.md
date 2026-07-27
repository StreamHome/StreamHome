# StreamHome Documentation

This documentation describes the supported `v0.1.0-alpha.1` Linux server release.

## Install and configure

1. [Installation](installation.md)
2. [Initial Setup](setup.md)
3. [Getting Started](getting-started.md)
4. [Google Drive Storage](google-drive.md)

## Operate the server

- [Adding Media](adding-media.md)
- [MediaSender Integration](mediasender.md)
- [Playback](playback.md)
- [Backup and Recovery](backup-and-recovery.md)
- [Updating](updating.md)
- [Troubleshooting](troubleshooting.md)
- [Security](security.md)
- [Browser Client](pwa.md)
- [Frequently Asked Questions](faq.md)

## Develop

- [Architecture](development/architecture.md)
- [API Development Notes](development/api.md)

## Alpha support contract

- Linux is the release-gated server platform.
- The catalog database is always `server/database.db`.
- Physical media is always under `server/media`.
- TOTP is the only supported two-factor mechanism.
- Google Drive uses StreamHome’s encrypted application-owned Rclone configuration.
- The browser client is responsive but is not an offline-capable PWA.

Report reproducible defects through [GitHub Issues](https://github.com/WaqSea/StreamHome/issues). Report vulnerabilities privately without publishing secrets or exploit details.
