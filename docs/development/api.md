# API Development Notes

The FastAPI application is served on loopback port 8000 and proxied by the web server under the same browser origin.

Important route families:

- `/api/setup/*`: first-run setup and Google Drive connection;
- `/api/auth/*`: cookie authentication, TOTP, sessions, and integration credentials;
- `/api/profiles/*`: profile management and PIN verification;
- `/api/add-movie`: scoped ingestion;
- `/api/playback/*` and `/api/stream/*`: playback preparation and delivery;
- `/api/system/*`: authenticated administration;
- `/media/*`: range-capable physical media delivery.

Use the generated FastAPI OpenAPI document from a development server for the exact schema. Security-sensitive mutations must use the existing authentication, same-origin, rate-limit, and recent-reauthentication dependencies.

Movie ingestion JSON must omit `season` and `episode`; sending either key with `null` is not equivalent.
