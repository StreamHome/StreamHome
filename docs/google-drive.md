# Connect Google Drive to StreamHome

StreamHome uses a Google OAuth **Web application** client to authorize Google Drive directly from `/setup`. Rclone runs only on the StreamHome server; you do not need to install it on your desktop or phone.

Before starting the Drive step, create or select a Google Cloud project, enable the Google Drive API, configure the OAuth consent screen, and create an OAuth client with the application type **Web application**.

Copy the exact **Authorized redirect URI** shown in StreamHome setup into the Google OAuth client. It has this form:

```text
https://your-streamhome-domain.example/api/setup/rclone/drive/callback
```

Then copy the client ID and client secret into `/setup`. If the OAuth application is External and still in Testing, add the Google account you will authorize as a test user. StreamHome will open Google's own consent page, then let you choose or create a Drive folder and run a temporary read/write/delete health check.

Never publish your client secret, Rclone configuration, authorization code, refresh token, setup cookie, or ingestion token.
