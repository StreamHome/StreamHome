# System Patterns

## System Architecture
- **API Layer:** FastAPI provides high-performance asynchronous endpoints (`routes/queue.py`, `routes/auth.py`, `routes/stream.py`).
- **Data Persistence:** `SQLModel` handles the SQLite ORM (Object Relational Mapping). Models are defined in `models.py`.
- **Background Processing:** A custom queue manager (`services/queue.py`) runs as a background task. It maintains a set of active tasks and processes them sequentially or concurrently.
- **Media Processing:** `services/ffmpeg.py` handles FFmpeg interactions via subprocesses to merge separate video and audio streams seamlessly.
- **External APIs:** `services/tmdb.py` interacts with The Movie Database API to enrich the application with comprehensive metadata.

## Key Technical Decisions
1. **Disk-to-Database Recovery Mechanism:**
   - **Decision:** The backend reads `.metadata/metadata.json` (or `metadata_sX_eY.json`) files inside media directories.
   - **Rationale:** Acts as a hard-disk registry written by the media sender system. If the SQLite database is deleted, the `lifespan` event on server startup scans the `media/` tree, parses these JSON files, and automatically rebuilds the database entries.
2. **Direct Image Caching:**
   - **Decision:** Images (`poster.jpg`, `backdrop.jpg`, `thumbnail.jpg`) are saved directly inside the respective media folder (e.g., `media/Movies/Movie_Name/poster.jpg`) instead of a centralized cache directory.
   - **Rationale:** Keeps media assets entirely portable and self-contained. If you move a movie folder, its poster moves with it.
3. **Dynamic Folder & File Renaming:**
   - **Decision:** When cataloging or recovering media, if the folder name on disk is a placeholder (starts with `Captured ` or matches default names) but rich TMDB metadata is successfully retrieved, the backend physically renames the directory on disk to match the corrected TMDB title (`Fight Club_1999_TMDB_550`) and renames the inner video file accordingly.
   - **Rationale:** Keeps folder layouts clean and readable, resolving anonymous capture names into human-readable directory structures.
4. **Sender-Provided Language Injection:**
   - **Decision:** The API allows the sender to specify the `language` of the media stream.
   - **Rationale:** This overrides TMDB's default `original_language`, accurately reflecting the dubbed or native language of the captured video file.
5. **Throttled Transient IPC Channel:**
   - **Decision:** Active download metrics (speeds, progress, ETA, and sizes) are shared between the background Uvicorn server and the CLI process via a throttled JSON file (`temp/download_metrics.json`) resolved relative to config.
   - **Rationale:** Prevents SQLite database write contention and lockups that would occur from high-frequency progress writes, while allowing a completely decoupled CLI process to read the stats.
6. **Frontend Multi-Tier Route Guarding:**
   - **Decision:** React Router paths are protected by a cascading chain of Context-aware wrappers (`AuthGuard`, `ProfileGuard`, `AdminGuard`).
   - **Rationale:** Ensures strict, highly predictable rendering blockades. An unauthenticated user cannot load the profile selector; a user without an active profile cannot load the dashboard; non-admins cannot load the CLI center.
7. **Design System Matrix (Theming):**
   - **Decision:** The web UI supports 4 core themes (Ember, Aurora, Cinema, Gemini) managed globally via Zustand (`themeStore.ts`) triggering `data-theme` HTML attributes.
   - **Rationale:** Keeps CSS entirely modular via mapped CSS variables instead of messy inline logic, while enabling users to fully customize their own profile aesthetics without interfering with backend configurations.
8. **PWA Offline Resilience:**
   - **Decision:** Integrated Vite Workbox with `StaleWhileRevalidate` caching strategies.
   - **Rationale:** Ensures the dashboard and static JS/CSS assets boot instantly on cellular networks or spotty Wi-Fi, while excluding `/media/` caching to prevent browser local storage limits from being exhausted.

## Design Patterns
- **Singleton Queue Manager:** `QueueManager` is instantiated once and injected/imported where needed.
- **Async Context Managers:** FastAPI's `lifespan` handles startup and teardown gracefully, ensuring the database is initialized and the queue manager is cleanly started and stopped.
