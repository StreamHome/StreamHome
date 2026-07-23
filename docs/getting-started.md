# Getting Started with StreamHome

This guide walks through the complete first-use experience, from installing StreamHome to playing your first title.

> [!IMPORTANT]
> StreamHome is currently in alpha.
>
> Installation steps, APIs, configuration formats, and supported environments may change between releases. Always follow the instructions included with the exact release you are installing.

## Before You Begin

You will need:

* a supported Windows or Linux server;
* administrator access to that server;
* a modern web browser;
* enough local storage for application data, temporary media processing, metadata, and cache files;
* an internet connection for metadata retrieval and optional Google Drive integration;
* media that you are authorized to access, process, store, and stream.

Google Drive is optional. StreamHome can operate using local storage only.

## 1. Install StreamHome

Open the [StreamHome Releases](https://github.com/StreamHome/StreamHome/releases) page and select the release you want to install.

Each release contains its own:

* installation instructions;
* supported operating systems;
* update notes;
* known limitations;
* migration information.

Always use the installer associated with the selected release rather than installation files from the mutable development branch.

After installation, StreamHome should display or provide the URL for its setup interface.

Depending on your environment, this may resemble:

```text
http://SERVER_IP:PORT/setup
```

or:

```text
https://your-streamhome-domain.example/setup
```

Use the exact address produced by your installation.

## 2. Open the Setup Wizard

Open the provided StreamHome address in your browser.

A fresh installation should direct you to:

```text
/setup
```

The setup interface remains protected until the required initial configuration has been completed.

Do not expose an unfinished installation publicly unless access is restricted through a trusted network, firewall, VPN, or reverse proxy.

## 3. Create the Administrator Account

The setup wizard will ask you to create the first administrator account.

Use a strong and unique password that is not shared with another service.

The administrator account can manage sensitive areas such as:

* server configuration;
* storage;
* backups;
* authentication;
* profiles;
* ingestion credentials;
* updates and maintenance.

Do not share administrator credentials with MediaSender clients or other integrations.

## 4. Configure Authentication

StreamHome may offer optional TOTP-based two-factor authentication during setup.

When enabling two-factor authentication:

1. Scan the displayed QR code using a compatible authenticator application.
2. Enter the generated verification code.
3. Save the provided recovery codes in a secure offline location.
4. Do not store recovery codes inside the StreamHome server directory.

Recovery codes are intended for account recovery if access to the authenticator is lost.

Each recovery code should be treated like a password.

## 5. Configure TMDB

StreamHome uses TMDB to retrieve media information such as:

* titles;
* descriptions;
* posters;
* backdrops;
* cast and crew;
* genres;
* production information;
* movie, series, season, and episode metadata.

Enter the required TMDB credentials when prompted by the setup wizard.

StreamHome does not use TMDB as a media source. TMDB is used only for catalog metadata and artwork.

## 6. Configure Media Ingestion

During setup, StreamHome creates or requests an ingestion credential for compatible MediaSender clients.

This credential should only be used for media-ingestion operations.

Keep it private and do not:

* publish it in screenshots;
* include it in Git repositories;
* paste it into public issues;
* share it with untrusted clients;
* reuse the administrator password as an ingestion credential.

Compatible clients can submit media through the StreamHome ingestion API.

Official browser integrations are maintained separately:

* [StreamHome Extension for Chrome](https://github.com/StreamHome/StreamHome-Extension-Chrome)
* [StreamHome Extension for Firefox](https://github.com/StreamHome/StreamHome-Extension-Firefox)

Other clients may also be developed using scripts, command-line applications, automation systems, or third-party integrations.

## 7. Choose a Storage Mode

StreamHome supports local storage and optional Google Drive integration.

### Local Storage

With local storage, finalized media remains on the StreamHome server.

You should select a directory with enough space for:

* media files;
* temporary downloads;
* FFmpeg processing;
* HLS cache;
* artwork;
* metadata;
* database backups.

Avoid using temporary system directories or locations that may be cleared automatically.

### Google Drive

Google Drive can be connected through the setup wizard.

StreamHome manages its Google Drive connection through Rclone and Google OAuth.

The setup process may include:

1. Google authentication;
2. Drive folder selection or creation;
3. permission verification;
4. read, write, and delete checks;
5. storage health validation.

StreamHome may keep application data locally while synchronizing finalized media and backups with Google Drive.

For detailed instructions, see [Google Drive Integration](google-drive.md).

## 8. Complete Setup

Before completing setup, review the configuration summary.

Confirm that:

* the administrator account is correct;
* authentication is configured as intended;
* TMDB connectivity succeeds;
* the ingestion credential has been saved securely;
* the local storage directory is writable;
* Google Drive passes its health checks if enabled;
* backup locations are valid.

After setup completes, StreamHome may restart its services.

Once the restart is complete, opening `/setup` should redirect to the login page rather than allowing setup to run again.

## 9. Sign In

Open your StreamHome address and sign in using the administrator account created during setup.

If two-factor authentication is enabled, enter the current code generated by your authenticator application.

After signing in, StreamHome will direct you to profile selection or profile creation.

## 10. Create or Select a Profile

Profiles keep viewing activity and personalization separate.

Profile-specific data may include:

* watch progress;
* playback history;
* watchlist;
* likes, loves, and dislikes;
* recommendations;
* search interactions;
* completion state;
* repeated viewing;
* Continue Watching;
* Watch Again.

Create at least one profile before using the main interface.

Do not use separate administrator accounts merely to separate viewing preferences. Profiles are designed for that purpose.

## 11. Explore the Interface

StreamHome includes four distinct interface themes:

* Ember;
* Aurora;
* Cinema;
* Gemini.

These themes may differ in:

* navigation;
* hero presentation;
* card layout;
* details pages;
* visual language;
* player presentation.

You can select the theme that best matches your preferred viewing experience.

The available catalog may initially contain metadata-only suggestions. A title becomes directly playable after compatible media has been successfully ingested and processed.

## 12. Add Your First Movie

Media can be sent to StreamHome using a compatible MediaSender client.

A movie ingestion request may include:

* TMDB ID;
* media type;
* video URL;
* separate audio URL;
* custom request headers;
* subtitles;
* language information;
* quality information;
* intro, recap, preview, or credit markers.

The exact fields depend on the client and source.

For a movie, season and episode values should not be included.

After submission, StreamHome places the request into its background queue.

## 13. Monitor Processing

StreamHome processes ingestion jobs asynchronously.

A typical job may include:

1. request validation;
2. source inspection;
3. video and audio retrieval;
4. FFmpeg or FFprobe processing;
5. subtitle handling;
6. media merging;
7. TMDB metadata retrieval;
8. artwork download;
9. local catalog creation;
10. optional Google Drive synchronization;
11. recovery metadata updates.

The queue should report useful status information if a job fails.

Transient failures may be retried. Invalid requests or permanently unavailable sources may require a corrected submission.

Do not restart the server repeatedly while an ingestion job is actively writing media unless troubleshooting instructions specifically require it.

## 14. Find the Added Title

After processing completes, return to the StreamHome catalog.

The new title should appear with:

* poster;
* backdrop;
* title and year;
* metadata;
* availability state;
* playback controls.

Use search if the title is not immediately visible.

A playable title should be distinguishable from a metadata-only catalog suggestion.

## 15. Open the Details Page

Select the title to open its details page.

Depending on the available metadata and profile state, the page may display:

* overview;
* cast and crew;
* genres;
* production information;
* recommendation reason;
* watchlist controls;
* reaction controls;
* playback state;
* resume position;
* available episodes for series.

Confirm that the title is shown as available before attempting playback.

## 16. Play the Title

Select **Play** or **Resume**.

StreamHome supports a secure playback flow with session-scoped authorization.

Depending on the source and client capabilities, playback may provide:

* adaptive fMP4 HLS;
* quality selection;
* audio-track selection;
* subtitles;
* playback-speed controls;
* fullscreen;
* picture-in-picture;
* seek controls;
* intro or recap skipping;
* credits and next-episode actions;
* progress tracking.

When supported, StreamHome may begin playback from an incoming source while local processing continues and later hand playback over to the managed copy without losing the current position.

## 17. Test Resume and Progress

For the first title:

1. Play for at least a short period.
2. Pause playback.
3. Leave the player.
4. Return to the details page.
5. Confirm that the resume position is available.
6. Resume playback.
7. Seek forward and backward.
8. Allow the title to reach completion if practical.

StreamHome tracks progress independently for each profile.

Completed titles may later appear in Watch Again or influence recommendations.

## 18. Restart the Server

After confirming playback:

1. Stop StreamHome using the supported procedure for your release.
2. Start StreamHome again.
3. Sign in.
4. Select the same profile.
5. Confirm that the following remain available:

   * accounts;
   * profiles;
   * catalog;
   * media availability;
   * watch progress;
   * watchlist;
   * settings;
   * storage configuration.

A normal restart should not require the setup wizard to run again.

## 19. Verify Backups

Open the administration or backup interface and confirm that backup destinations are configured correctly.

Before relying on a backup system, verify:

* the backup is actually created;
* its timestamp is current;
* local backup files are accessible;
* Google Drive backup succeeds if enabled;
* failed backups report an error;
* restore instructions are understood before an emergency occurs.

For details, see [Backup and Recovery](backup-and-recovery.md).

## 20. Install the Progressive Web App

On supported browsers, StreamHome can be installed as a Progressive Web App.

The exact installation option depends on the browser and operating system.

It may appear as:

* Install app;
* Add to Home Screen;
* Create shortcut;
* Install StreamHome.

After installation, StreamHome can open in a standalone application-style window while continuing to receive its interface from your server.

Some offline functionality may be available depending on the feature, device, browser, and StreamHome release.

For more information, see [Progressive Web App](pwa.md).

## Recommended First-Run Checklist

After completing this guide, verify that you can:

* [ ] open StreamHome;
* [ ] complete setup;
* [ ] sign in;
* [ ] create and select a profile;
* [ ] submit a movie;
* [ ] monitor its queue status;
* [ ] find it in the catalog;
* [ ] open the details page;
* [ ] start playback;
* [ ] pause and seek;
* [ ] select subtitles or audio when available;
* [ ] leave and resume playback;
* [ ] restart the server;
* [ ] retain accounts, profiles, catalog data, and progress;
* [ ] create a valid backup.

## Common First-Run Problems

### `/setup` does not open

Confirm that:

* StreamHome services are running;
* the installation completed successfully;
* the correct port is open;
* a reverse proxy is not routing to the wrong service;
* setup has not already been completed.

If setup is complete, `/setup` should normally redirect to `/login`.

### Login fails after setup

Confirm that:

* the correct administrator username is being used;
* the password is entered exactly;
* the system time is correct if TOTP is enabled;
* browser cookies are allowed;
* the current StreamHome service is running rather than an older development process.

### Media submission fails

Check:

* the ingestion credential;
* client connection settings;
* required request fields;
* URL format;
* media availability;
* subtitle and audio URLs;
* queue error details.

Do not include private credentials or signed URLs in public issue reports.

### Metadata does not appear

Check:

* TMDB credentials;
* TMDB network connectivity;
* the submitted TMDB ID;
* artwork and metadata cache status;
* server logs for a specific metadata error.

### Playback does not start

Check:

* whether the title is marked as playable;
* whether processing completed;
* local or Google Drive source availability;
* FFmpeg availability;
* browser compatibility;
* playback error details;
* whether the selected media format requires preparation.

### Google Drive connection fails

Check:

* OAuth Client ID and secret;
* OAuth application type;
* authorized redirect URI;
* Drive permissions;
* selected folder;
* Rclone configuration;
* server time;
* HTTPS and reverse-proxy settings.

The redirect URI must exactly match the value configured in Google Cloud, including the scheme, domain, path, and trailing slash behavior.

## Getting Help

If the problem remains:

1. Read the [FAQ](faq.md).
2. Review [Troubleshooting](troubleshooting.md).
3. Search existing [GitHub Issues](https://github.com/StreamHome/StreamHome/issues).
4. Open a new issue with reproducible steps and sanitized logs.

Include:

* StreamHome version;
* operating system;
* browser and device;
* storage mode;
* installation method;
* expected behavior;
* actual behavior;
* exact reproduction steps.

Never publish:

* passwords;
* ingestion credentials;
* session tokens;
* OAuth secrets;
* cookies;
* signed media URLs;
* recovery codes;
* private server addresses;
* personal media information.

## Next Steps

Continue with:

* [Installation](installation.md)
* [Initial Setup](setup.md)
* [Adding Media](adding-media.md)
* [Google Drive Integration](google-drive.md)
* [Playback Overview](playback.md)
* [Backup and Recovery](backup-and-recovery.md)
* [Updating StreamHome](updating.md)
* [Frequently Asked Questions](faq.md)

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
