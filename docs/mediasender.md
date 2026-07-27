# MediaSender Integration

MediaSender clients authenticate with a scoped ingestion token, not an administrator password or browser session.

## Credential handling

- Copy the one-time ingestion token when setup completes.
- Store it in the sender’s secret store.
- Never place it in URLs, screenshots, logs, or source control.
- Rotate or revoke it from the Admin security page when exposure is suspected.

## Request contract

Send requests to `/api/add-movie` through the StreamHome web origin. For movies, completely omit `season` and `episode`. For episodes, provide valid numeric values.

The server validates source addressing, queues processing, probes media with FFmpeg, writes portable metadata, and finalizes the catalog record only after required output exists.
