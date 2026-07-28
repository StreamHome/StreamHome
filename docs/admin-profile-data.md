# Profile Data Administration

The **Admin center → Profile data** page lets a recently reauthenticated administrator inspect every StreamHome profile without entering, unlocking, or impersonating it.

Use the **Selected profile** dropdown to choose the profile whose data should be displayed. The selection is stored in the admin URL, so refreshing the page keeps the same inspection target.

## Available views

- **Overview** summarizes watch time, viewing attempts, resume states, saved titles, recommendation records, activity, and where each kind of data is stored.
- **History** separates chronological viewing attempts from the latest Continue Watching positions.
- **Watchlist** shows the server-ordered saved titles and their creation times.
- **Recommendations** shows the persisted candidate pool, scores, reasons, candidate sources, explicit feedback, onboarding genres, learned tastes, refresh state, and persisted shadow-comparison metrics.
- **TMDB cache** shows shared metadata-only titles associated with the selected profile, why they are associated, cache health, retry state, and whether poster, backdrop, and portable metadata files exist on disk.
- **Activity** shows accepted interaction telemetry, visibility-qualified recommendation exposures, and recent playback-run lifecycle records.

## Data ownership and storage

Profile-owned activity is stored in the standardized `server/database.db` database. This includes history, resume state, watchlists, recommendations, tastes, feedback, telemetry, exposures, and playback runs.

TMDB catalog rows are shared server resources. Their indexed metadata is stored in SQLite, while downloaded artwork and portable `.metadata/metadata.json` files are stored under `server/media`. Removing a profile does not remove shared TMDB catalog rows or physical cache files.

Playback preparation, transcode, and subtitle caches under `server/temp` are temporary server resources and are not attributed to an individual profile.

Recommendation exposures are retained in a bounded browser delivery queue until the server acknowledges them. A successfully acknowledged exposure is durable in SQLite. Failed delivery is retried, including after a page reload.

## Profile deletion

Deleting a profile removes its profile-owned playback, history, watchlist, telemetry, preference, taste, recommendation, and refresh records in the same database transaction. Shared movies, series, TMDB cache entries, artwork, and playable media are preserved.
