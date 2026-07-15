# Active Context

## Current Focus
- Preparing for the final stretch of backend features: **Rclone Upload Progress Tracking** and **Automatic Subtitle Auto-Conversion (VTT to SRT)**, now that the Web UI and server architectures are fully stable.

## Recent Changes
- **Server Startup & Lifecycle Optimization:**
  - Converted the blocking `await queue_manager.sync_media_from_disk()` call in the FastAPI lifespan startup hook into an asynchronous background task using `asyncio.create_task()`. This prevents the server process from blocking during boot (which was causing Nginx to throw `504 Gateway Timeout` errors because the startup hook was waiting for full disk scanning and audio extractions to finish).
- **Server Codebase Stabilizations (July 2026):**
  - Implemented a 24/7 background HEVC Compression Engine (`services/hevc.py`) that compresses local videos to `libx265` to save 50% disk space, featuring user kill-switches and a 4-core hardware auto-guard. Integrated Admin controls into the React Settings UI.
  - Fixed a critical `sync_media_from_disk` crash where placeholder media files failed the strict physical file check, restoring graceful metadata recovery without failing database cataloging.
  - Resolved SQLAlchemy `greenlet_spawn` async errors occurring when restoring missing files into the database during server startup tests.

  - Resolved live download progress monitoring in the CLI dashboard by opening the FFmpeg subprocess in binary mode (removing `text=True` and `errors="ignore"`) and reading unbuffered bytes directly from `process.stderr` to bypass Python text-wrapper block-buffering entirely.
  - Added immediate task metric initialization on download start and state transitions, forcing immediate metric flushes to disk (bypassing the 1.0s write throttle).
  - Resolved `NameError: should_cache` crashes in `routes/stream.py` transcoding by defining it as a default param.
  - Fixed a missing `json` import in `main.py` that crashed cloud media serving.
  - Replaced the async `await` on synchronous `subprocess.Popen.wait()` in `services/state.py` with thread executor calls.
  - Moved metrics cleanup to the `finally` block in `services/queue.py` to prevent zombie download task states.
  - Corrected series directory rclone folder naming schema in `services/queue.py` to prevent nested season folder flattening.
  - Added the `extracted_languages` propagation to the media cataloging logic in `services/queue.py`.
  - Restricted rclone commands in the queue worker to run only when the storage engine is set to `CLOUD`.
  - Fixed duplicate `-headers` injection in `services/ffmpeg.py` for separate audio streams.
  - Resolved CPU and DB thrashing inside the SSE queue tracker in `routes/queue.py` by removing query bypass clauses on active tasks.
  - **Decoupled TheIntroDB Server Integration:** To comply with TheIntroDB's terms, the server no longer fetches skip markers directly. The fetching logic has been fully shifted to the client-side Media Sender Extension, which passes the `skip_markers` directly into the `/api/add-movie` ingestion payload. The backend `QueueManager` now saves these markers identically to ensure skip intro UI and auto-credits exit functionality remains completely unaffected for viewers.
- **Obfuscated HLS Ingestion & FFmpeg Enhancements:**
  - Configured `allowed_extensions ALL` and `extension_picky 0` bypass flags on both the media scanner (`media_probe.py`) and downloader (`ffmpeg.py`) to successfully ingest HLS streams disguised with `.jpg` segment extensions.
  - Implemented real-time download speed calculation (in Mbps/MBps) and downloaded file size tracking (in MB/GB) inside `ffmpeg.py` by monitoring the physical file size changes on disk.
  - Enforced line-buffering (`bufsize=1`) on FFmpeg subprocess `Popen` executions, preventing Linux block-buffering from withholding progress updates.
  - Standardized time regex to support 3-digit millisecond time formats (`time=00:00:00.000`) in newer FFmpeg versions, resolving parsing omissions.
- **Proxy Configuration Optimization:**
  - Replaced `localhost` target hosts with explicit loopback IPv4 `127.0.0.1` in both `web/server.ts` and `web/vite.config.ts`. This resolves silent proxy connection issues on servers where Node.js 17+ defaults to IPv6 (`::1`) while FastAPI binds to IPv4 (`127.0.0.1` / `0.0.0.0`).
- **CLI Download Monitoring TUI Dashboard Upgrades & Process Controls:**
  - Optimized `stop.sh` with a port-based killing fallback (targeting ports 8000, 3000, and 24678) to ensure that backend FastAPI and orphaned Vite/Node processes are reliably terminated even when process text-matching fails.
  - Optimized `start.sh` to automatically run `stop.sh` on startup to clean dangling processes and changed `python3` to `python` inside virtual environment contexts to guarantee correct module resolves.
  - Implemented change-based redraw detection in Option 3 ("Monitor Active Download Queue & Workers"). The screen is only cleared and redrawn when a difference in the database task attributes or transient active download metrics is detected, eliminating terminal screen flickering and reducing CPU cycles.
  - Enhanced the status output column for `FAILED` tasks to display the specific truncated error messages (e.g. expired URL, HTTP errors) directly below the `✗ Failed` text in red.
  - Refactored option `3` ("Monitor Active Download Queue & Workers") to dynamically auto-refresh every 1 second, drawing live speed, percentage, file size, and ETA metrics.
  - Shared transient download states between Uvicorn and CLI using a throttled IPC JSON file bridge (`download_metrics.json`) resolved absolutely via `config_dir`.
  - Added terminal echo disabling (`ECHO` and `ICANON`) on Unix systems during active TUI monitoring loops to prevent keypress characters from leaking onto the console.
  - Implemented robust `Q`/`q` and `X`/`x` fallback keyboard exit controls, allowing users to return cleanly to the main menu without relying solely on SSH-prone `ESC` latency sequences.
- **2FA Security Hardening (TOTP & Lockouts):**
  - Integrated `pyotp` into the authentication backend (`routes/auth.py`).
  - Added user account failed login tracking and lockout mechanisms (15-minute lockouts after 5 consecutive password or verification failures) protecting against brute force.
  - Upgraded the Admin Control Center (`cli.py`) with a dedicated "Manage Users & 2FA Security Center" sub-menu supporting new registrations, password resets, 2FA setups, and administrative account unlocks.
  - Completely removed the old SMTP email/OTP verification systems, dependencies (`aiosmtplib`), and settings.
  - Implemented client-side Login Screen and secure TOTP input dialogs with high-fidelity passcode entry cells.
  - Added a responsive 2FA configuration card widget inside the client settings tab allowing users to view status, register TOTP keys via QR codes (using a fallback QR Server API), verify setups, or disable active 2FA parameters.

- **Dynamic Rclone Settings Toggle & Fallback Resilience:** Designed a persistent settings system utilizing `settings.json` (disabled by default) to toggle the storage engine dynamically between `LOCAL` and `CLOUD`. Integrated toggle switches and subpath input fields in Settings view with automatic save-on-blur. Implemented automated fallback handlers that redirect assets from temporary folders to local media catalog directories if the cloud upload pipeline crashes.
- **CLI Storage Settings TUI & Account Management:** Refactored `cli.py` storage engine and remote path prompts into an arrow-selectable sub-menu displaying values dynamically. Renamed User Center to "Account Management", removed the "Unlock User" option, and skipped email prompts for password resets/2FA setup, automatically targeting the registered default admin account.
- **Automated Setup Wizard & Dependency Auto-Installers:** Created `setup.bat` (Windows) and `setup.sh` (Linux/macOS) to install Python 3.11, Node.js 20, local portable **FFmpeg/FFprobe** binaries, and **Rclone**. Implemented an interactive CLI TUI wizard (`cli.py --setup`) featuring dynamic ASCII banner parsing, admin register/2FA setup, verified TMDB token checks against Movie ID `290250` via httpx, and storage setup.
- **Global Structured Logging & Print Cleanup:** Integrated standard python logging using `RotatingFileHandler` writing logs to `server/temp/app.log` (5MB max size, 3 backup files) and console stream, replacing all raw `print()` statements in route/service logic.
- **Database Schema Auto-Migrations:** Added `error_message`, `has_video`, `has_audio`, and `scan_quality` fields to `DownloadTask`, with dynamic startup auto-migrations in `db.py`.
- **Media Ingestion & Stream Scanner:** Enforced 5GB disk checking, ffprobe stream media component probing, and non-blocking REST notifications containing metadata sent to `VIDEO_SENDER_API_URL`. Implemented worker task retry loops with exponential backoff.
- **Startup Integrity Cleanups:** Added database cleanup hooks in `main.py` on startup to reset any dangling tasks in `DOWNLOADING`, `MERGING`, or `MOVING_CLOUD` to `FAILED` with an "Interrupted by server shutdown/restart" error log.
- **Transcode File Caching:** Created segment segment/file caching in `routes/stream.py` under `server/temp/transcode_cache/`. Cached dynamic transcode output is served directly via `FileResponse` for range-seek support.
- **Admin TUI Upgrades (cli.py):** Added setting options to view, set, or clear `VIDEO_SENDER_API_URL` and implemented a fully interactive task scrolling list and detailed inspector pane in the Monitor Queue panel.
- **Web Phase 11 (UI Bug Fixes):**
  - Resolved severe z-index click interception issues on the `EmberHome` hero section caused by an invisible absolute gradient overlay lacking `pointer-events-none`.
  - Purged overlapping ambient background canvases that were duplicating rendering loops and severely degrading performance.
  - Restored missing mock fallback images (poster and backdrop) globally by linking them directly into the Vite public asset directory to ensure resilient localhost mapping.

## Next Steps
- Continue verifying production build size optimizations and client-side page load times under low-bandwidth simulation.
- Prepare the implementation plan for **Rclone Upload Progress Tracking** or **Automatic Subtitle Auto-Conversion (VTT to SRT)** when ready.
