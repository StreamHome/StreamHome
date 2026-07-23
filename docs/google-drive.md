# Google Drive Integration

This guide explains how to create a Google Cloud project, enable the Google Drive API, configure OAuth, and connect a Google Drive account to StreamHome.

No manual Rclone terminal configuration is required. StreamHome manages its own Rclone configuration after Google OAuth has been configured successfully.

> [!IMPORTANT]
> Google Cloud Console changes occasionally. Labels or button positions may differ slightly, but the required configuration remains the same.
>
> Never publish your OAuth Client Secret, refresh token, Rclone configuration, or StreamHome credentials.

## What You Will Configure

The complete process is:

1. Create or select a Google Cloud project.
2. Enable the Google Drive API.
3. Configure the Google Auth Platform.
4. Set the application audience and add a test user.
5. Configure the required Google Drive scope.
6. Create a Web application OAuth client.
7. Register StreamHome's callback URL.
8. Enter the OAuth credentials into StreamHome.
9. Authorize the Google account.
10. Select or create a Google Drive folder.
11. Complete StreamHome's storage health check.

## Before You Begin

You need:

- a Google account;
- an installed StreamHome server;
- access to the StreamHome setup wizard or Google Drive settings;
- an HTTPS domain for an internet-accessible StreamHome installation;
- permission to configure the selected Google Cloud project.

For a public server, your StreamHome address should resemble:

`https://watch.example.com`

StreamHome's Google OAuth callback follows this structure:

`https://YOUR_STREAMHOME_DOMAIN/api/setup/rclone/drive/callback`

Do not guess the callback address. StreamHome displays the exact redirect URI that must be registered in Google Cloud.

---

## 1. Open Google Cloud Console

Open:

`https://console.cloud.google.com/`

Sign in with the Google account that will own the StreamHome OAuth project.

<img width="1919" height="1079" alt="Google Cloud Console home page with the project selector highlighted" src="https://github.com/user-attachments/assets/fd4db0f5-1be4-47a3-a24e-f498fc90e3fd" />

## 2. Open the Project Selector

Select the project name or **Select a project** in the top navigation bar.

The project-selection dialog will open.

<img width="1919" height="1079" alt="Google Cloud project-selection dialog with New Project highlighted" src="https://github.com/user-attachments/assets/49bf21ba-1637-4707-8875-97a5af557901" />

Select **New Project**.

## 3. Create a Google Cloud Project

Enter a clear project name such as:

`StreamHome`

If Google asks for an organization or location and you do not use Google Workspace, leave it as:

`No organization`

Select **Create**.

<img width="1919" height="1079" alt="Google Cloud new-project form configured for StreamHome" src="https://github.com/user-attachments/assets/e77afcdd-8f3d-4e92-856f-330decfd10db" />

Wait until Google finishes creating the project, then select the new **StreamHome** project from the project selector.

> [!IMPORTANT]
> Confirm that the correct project is selected before continuing. APIs, OAuth settings, and client credentials are created inside the currently selected project.

## 4. Open the API Library

Open the main navigation menu and go to:

**APIs & Services → Library**

You can also use the search bar at the top of Google Cloud Console and search for:

`Google Drive API`

<img width="1919" height="1079" alt="Google Cloud navigation showing APIs and Services Library" src="https://github.com/user-attachments/assets/8d39a4ca-3969-4ad5-ba39-a8e2a42cfa9f" />

## 5. Enable the Google Drive API

In the API Library, search for:

`Google Drive API`

<img width="1919" height="1079" alt="Google Cloud API Library search for Google Drive API" src="https://github.com/user-attachments/assets/d410fcf7-98e6-47a3-a24e-f498fc90e3fd" />

Open the result named **Google Drive API**.

<img width="1919" height="1079" alt="Google Drive API result selected in Google Cloud" src="https://github.com/user-attachments/assets/43ab33e6-372e-4367-a358-3e8075e74dec" />

Select **Enable**.

<img width="1919" height="1079" alt="Google Drive API page with Enable highlighted" src="https://github.com/user-attachments/assets/4f8d7165-a132-4e6a-95ba-f12a31b7d038" />

After the API has been enabled, Google may redirect you to the API overview page.

You do not need to configure Drive UI integration or publish an application in the Google Workspace Marketplace.

## 6. Start Google Auth Platform Setup

Open the navigation menu and go to:

**APIs & Services → OAuth consent screen**

Google may redirect this page to the newer **Google Auth Platform** interface.

<img width="1919" height="1079" alt="Google Cloud navigation showing OAuth consent screen" src="https://github.com/user-attachments/assets/3fca4fae-54bd-4258-aeb9-00d64823fb99" />

Select **Get Started**.

<img width="1919" height="1070" alt="Google Auth Platform welcome page with Get Started highlighted" src="https://github.com/user-attachments/assets/5e98d00a-b1b9-4da4-9a9d-9ba75bb4cac3" />

### App Information

Enter:

- **App name:** `StreamHome`
- **User support email:** your Google account or support email

Continue to the next step.

<img width="1919" height="1079" alt="Google Auth Platform app-information form configured for StreamHome" src="https://github.com/user-attachments/assets/ae102d2b-4ab5-435e-b194-8b0a1fdfdbfc" />

### Audience

For a normal personal Google account, select:

`External`

The **Internal** option is intended for eligible Google Workspace organizations.

Continue to the next step.

<img width="1919" height="1079" alt="Google Auth Platform audience selection with External highlighted" src="https://github.com/user-attachments/assets/1505fdcf-161b-4a53-851b-7b869d7d92cd" />

### Contact Information

Enter an email address that Google can use for OAuth and project notifications.

<img width="1919" height="1079" alt="Google Auth Platform contact-information form" src="https://github.com/user-attachments/assets/49f23098-1431-43b2-a7a2-01813db07435" />

### Finish the Initial Configuration

Review the information, accept the Google API Services User Data Policy when you agree with its terms, and continue.

<img width="1919" height="1079" alt="Google Auth Platform policy acknowledgement and continuation step" src="https://github.com/user-attachments/assets/0433cad7-7857-467d-9137-77fdbd29f256" />

Select **Create** to finish the initial Google Auth Platform configuration.

<img width="1919" height="1079" alt="Google Auth Platform setup summary with Create highlighted" src="https://github.com/user-attachments/assets/0bfd84e5-529b-4b0a-9649-2f9f43e69c2e" />

## 7. Configure Branding

Open:

**Google Auth Platform → Branding**

Confirm or complete the information displayed during Google authorization.

| Field | Recommended value |
|---|---|
| **App name** | `StreamHome` |
| **User support email** | Your StreamHome support email or Google account |
| **App logo** | Optional |
| **Developer contact information** | Your contact email |

<img width="1919" height="1079" alt="Google Auth Platform Branding page showing StreamHome application details" src="https://github.com/user-attachments/assets/e507a6a9-38c1-47b9-8c3e-157cd8b34ea7" />

Google may also display fields for:

- application home page;
- privacy policy;
- terms of service;
- authorized domains.

### Authorized Domains

Add the root domain used by the StreamHome installation.

For example, if StreamHome is available at:

`https://watch.example.com`

enter:

`example.com`

Do not enter:

- `https://example.com`;
- `watch.example.com`;
- a port;
- a URL path;
- the OAuth callback endpoint.

The complete callback URL will be added separately under **Clients → Authorized redirect URIs**.

Complete all required fields and select **Save**.

<img width="1919" height="1079" alt="Google Auth Platform Branding page showing authorized domains and save controls" src="https://github.com/user-attachments/assets/a13224ea-6159-479a-bc06-c18c677f78b5" />

## 8. Configure the Audience and Add a Test User

Open:

**Google Auth Platform → Audience**

Confirm that the user type is:

`External`

During initial testing, keep the publishing status as:

`Testing`

Under **Test users**, select **Add users**.

Enter the Google account that will be connected to StreamHome, then select **Save**.

Example:

`example@gmail.com`

<img width="1919" height="1079" alt="Google Auth Platform Audience page showing the Add users dialog" src="https://github.com/user-attachments/assets/a9495429-7264-4f43-b0f3-69c60078f063" />

> [!IMPORTANT]
> While the application remains in Testing mode, the Google account used during StreamHome authorization must be listed under **Test users**.
>
> Otherwise, Google may block authorization even if the OAuth Client ID and redirect URI are correct.

## 9. Configure Data Access

Open:

**Google Auth Platform → Data Access**

Select **Add or Remove Scopes**.

<img width="1919" height="1079" alt="Google Auth Platform Data Access page with Add or Remove Scopes highlighted" src="https://github.com/user-attachments/assets/03b96a59-400d-4480-8d61-05899193f473" />

Select the Google Drive scope used by the StreamHome release.

For the complete Drive integration documented here, select:

`https://www.googleapis.com/auth/drive`

This permits the configured StreamHome server to perform Drive operations such as:

- browsing and creating folders;
- uploading and reading media;
- verifying uploaded files;
- deleting StreamHome-managed files;
- storing and restoring cloud backups.

Select **Update**, then save the Data Access configuration.

<img width="1919" height="1079" alt="Google Drive full-access OAuth scope selected in Data Access" src="https://github.com/user-attachments/assets/90d6b76e-c0f7-4519-b803-380288ef2075" />

Do not select additional overlapping Drive scopes unless a StreamHome release explicitly requests them.

> [!CAUTION]
> This Drive scope provides broad access to the connected Google Drive account.
>
> Consider using a dedicated Google account or a Drive account that does not contain unrelated personal files.

## 10. Open the Clients Page

Open:

**Google Auth Platform → Clients**

This page contains the OAuth 2.0 Client IDs belonging to the selected Google Cloud project.

Select **Create Client**.

<img width="1919" height="1079" alt="Google Auth Platform Clients page with Create Client highlighted" src="https://github.com/user-attachments/assets/8a3e8992-a9d2-4695-ba7c-f810353bd8de" />

## 11. Create a Web Application Client

For **Application type**, select:

`Web application`

Do not select **Desktop app** for StreamHome's domain-based server callback.

Enter a clear name such as:

`StreamHome Web`

### Add the Authorized Redirect URI

Return temporarily to the StreamHome setup wizard and locate the redirect URI displayed on the Google Drive configuration step.

It should resemble:

`https://watch.example.com/api/setup/rclone/drive/callback`

Copy the complete value exactly as displayed.

Return to the Google OAuth client form. Under **Authorized redirect URIs**, select **Add URI** and paste the exact callback URI shown by StreamHome.

<img width="1919" height="1079" alt="Web application OAuth client configured with the StreamHome redirect URI" src="https://github.com/user-attachments/assets/21668230-86cc-492e-bee4-4668c933a08b" />

> [!IMPORTANT]
> The registered redirect URI must match the URI sent by StreamHome exactly.
>
> The following differences matter:
>
> - `http` versus `https`;
> - domain and subdomain;
> - port;
> - complete callback path;
> - trailing slash.

For example, these are different:

- `https://watch.example.com/api/setup/rclone/drive/callback`
- `http://watch.example.com/api/setup/rclone/drive/callback`
- `https://example.com/api/setup/rclone/drive/callback`
- `https://watch.example.com/api/setup/rclone/drive/callback/`

A mismatch produces:

`Error 400: redirect_uri_mismatch`

### Authorized JavaScript Origins

StreamHome uses a server-side OAuth callback, so an Authorized JavaScript Origin is normally unnecessary unless the current StreamHome release explicitly asks for one.

When required, enter only the origin:

`https://YOUR_STREAMHOME_DOMAIN`

Do not include the callback path.

## 12. Create and Save the OAuth Client

Review the configuration and confirm that:

- the application type is **Web application**;
- the client name is clear;
- the redirect URI exactly matches StreamHome;
- the correct Google Cloud project is selected.

Select **Create**.

Google will generate:

- a Client ID;
- a Client Secret.

<img width="1919" height="1079" alt="Google OAuth client-created dialog showing Client ID and Client Secret locations" src="https://github.com/user-attachments/assets/db24d964-f858-45f0-8ceb-76fba6feba35" />

> [!WARNING]
> Copy or download the OAuth credentials and store them securely.
>
> Google may not display an existing Client Secret in full again. If it is lost, create a new secret and update StreamHome.

Never:

- publish the Client Secret;
- commit it to Git;
- include it in screenshots;
- paste it into public issues;
- share it with another user.

## 13. Enter the Credentials in StreamHome

Return to the StreamHome setup wizard.

Enter:

- **Google OAuth Client ID**
- **Google OAuth Client Secret**

Confirm that the redirect URI shown by StreamHome is the same URI registered in Google Cloud.

Select the button used to connect or authorize Google Drive.

<img width="1919" height="1079" alt="StreamHome Google Drive setup form showing OAuth credential fields" src="https://github.com/user-attachments/assets/72ec076f-d7e8-477a-9cbb-e57ec5d3cc5f" />

## 14. Authorize the Google Account

StreamHome redirects the browser to Google.

Select the same Google account that was added under:

**Audience → Test users**

Review the requested permissions and approve the connection only when:

- the application name is **StreamHome**;
- the selected Google account is correct;
- the requested Drive access matches the configuration;
- the redirect domain belongs to your StreamHome installation.

Google will redirect the browser back to:

`/api/setup/rclone/drive/callback`

StreamHome validates the OAuth response and stores the resulting authorization in its managed Rclone configuration.

## 15. Select or Create a Drive Folder

After authorization succeeds, StreamHome displays the Google Drive folder browser.

You can:

- select an existing folder;
- create a new folder;
- choose the folder StreamHome will manage.

A clear folder name is recommended:

`StreamHome`

StreamHome may create its own media and backup structure inside the selected folder.

Avoid selecting a folder containing unrelated important data unless you understand how the current release manages files.

## 16. Run the Storage Health Check

StreamHome performs a Drive health check before completing configuration.

The test may verify:

- authentication;
- folder access;
- file creation;
- file reading;
- file deletion;
- Rclone communication;
- Drive quota and permissions.

Wait for the health check to complete.

Do not continue when the health check reports a failure. Open the error details and correct the affected configuration first.

## 17. Complete Setup

After the Drive health check succeeds:

1. Confirm the selected folder.
2. Review the storage settings.
3. Save the configuration.
4. Complete the remaining StreamHome setup steps.
5. Allow StreamHome to restart when requested.

After restart, open the Admin Center or storage settings and confirm that Google Drive reports a healthy connection.

## How StreamHome Uses Google Drive

Depending on configuration and release, StreamHome may use Google Drive for:

- finalized media storage;
- cloud playback;
- verified local-to-cloud uploads;
- database backups;
- recovery synchronization.

StreamHome verifies cloud uploads before deleting a local media copy when local deletion is enabled.

Application data, temporary processing files, cache data, and active configuration may still remain on the local server.

## Recommended Google Drive Practices

### Use a Dedicated Folder

Keep StreamHome files inside a dedicated folder rather than the root of a Drive containing unrelated files.

### Consider a Dedicated Account

For stronger separation, use a dedicated Google account or Drive intended for StreamHome.

This limits the unrelated data visible to the configured OAuth connection.

### Protect OAuth Credentials

Treat the Client Secret and refresh token like passwords.

Anyone who obtains valid Drive credentials may be able to access the permissions granted to StreamHome.

### Maintain Local Backups

Google Drive synchronization is not a replacement for all backups.

Maintain current copies of:

- the StreamHome database;
- critical configuration;
- recovery information;
- OAuth configuration details;
- administrator recovery codes.

## Troubleshooting

### Error 400: `redirect_uri_mismatch`

Google received a callback URI that is not registered exactly for the OAuth Client ID.

The error page may display:

`Request details: redirect_uri=https://watch.example.com/api/setup/rclone/drive/callback`

Copy the value after `redirect_uri=` and add that exact value under:

**Google Auth Platform → Clients → StreamHome Web → Authorized redirect URIs**

Also confirm:

- StreamHome is using the correct Web Client ID;
- the client belongs to the selected Google Cloud project;
- HTTPS is not being changed to HTTP by the reverse proxy;
- the callback does not contain an unexpected port;
- there is no missing or additional trailing slash.

### Access blocked: This app's request is invalid

Check:

- the client type is **Web application**;
- the redirect URI is registered;
- the Client ID and Client Secret belong to the same client;
- StreamHome is not using an older Desktop client.

### Access denied or user not allowed

When publishing status is **Testing**, only configured test users can authorize the application.

Open:

**Google Auth Platform → Audience → Test users**

Add the Google account being used.

### Authorization stops working after the testing period

Review the publishing status under:

**Google Auth Platform → Audience**

For a long-running installation, move the project to **In production** when appropriate and complete any Google requirements that apply to the selected scopes and audience.

### Unverified app warning

Confirm that:

- you created the project;
- the Client ID matches your project;
- the displayed redirect domain is your StreamHome domain;
- the requested permissions are expected.

Do not continue for a project or domain you do not recognize.

### Client Secret is no longer visible

Create a new Client Secret in the OAuth client and update StreamHome with the replacement value.

Revoke or delete the previous secret when it is no longer required.

### Google Drive API is disabled

Open:

**APIs & Services → Library → Google Drive API**

Select **Enable**.

Confirm that the API is enabled in the same project that owns the OAuth Client ID.

### Folder listing fails

Check:

- Google Drive API is enabled;
- authorization completed successfully;
- the required Drive scope is configured;
- Rclone received valid authorization;
- the selected account has access to the folder;
- the server can reach Google APIs;
- the token has not expired or been revoked.

### Health check cannot create or delete a test file

The selected scope or folder permission may be insufficient.

Confirm that the connected account can create and delete files inside the selected folder.

### OAuth callback opens the wrong StreamHome server

Check the registered redirect URI.

It must use the same domain as the StreamHome server currently being configured.

Remove obsolete callback addresses when they are no longer used.

### Google Drive works locally but not through the public domain

Check the reverse proxy configuration:

- forwarded host;
- forwarded protocol;
- HTTPS termination;
- public base URL;
- callback path forwarding;
- trusted proxy configuration.

StreamHome must generate the public HTTPS callback rather than an internal HTTP address.

## Security Checklist

Before completing Google Drive setup, verify:

- [ ] The Google Cloud project belongs to you.
- [ ] Google Drive API is enabled.
- [ ] The OAuth audience is set correctly.
- [ ] The connected account is added as a test user when required.
- [ ] Only the required Drive scope is configured.
- [ ] The OAuth client type is Web application.
- [ ] The callback URI exactly matches StreamHome.
- [ ] The Client Secret is stored privately.
- [ ] Credentials are absent from Git and screenshots.
- [ ] The selected Drive folder is appropriate.
- [ ] StreamHome's Drive health check succeeds.
- [ ] A current local database backup exists.

## Related Documentation

- [Getting Started](getting-started.md)
- [Initial Setup](setup.md)
- [Storage Overview](storage.md)
- [Backup and Recovery](backup-and-recovery.md)
- [Troubleshooting](troubleshooting.md)
- [Security](security.md)

---

<p align="center">
  <b>Your media. Your server. Your StreamHome.</b>
</p>
