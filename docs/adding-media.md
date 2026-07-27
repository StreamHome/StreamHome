# Adding Media

Media is ingested through a scoped MediaSender integration credential created during setup or rotated in the Admin security page.

The server accepts ingestion requests at `/api/add-movie`. Movie requests must omit the `season` and `episode` JSON keys completely. Do not send those keys with `null` values.

Episode ingestion includes valid season and episode numbers. Completed media is stored under the absolute `server/media` directory and is published by FastAPI at `/media` with range-request support.

Every completed movie or episode must have adjacent `.metadata/metadata.json` containing:

- `quality`;
- `languages` as a list;
- `subtitles` as a list of language/extension dictionaries.

Only ingest media that you are authorized to store and stream.
