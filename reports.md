# StreamHome Security Audit Report

**Audit Date:** 2026-07-12  
**Auditor Role:** Senior Cybersecurity Engineer  
**Scope:** StreamHome Media Server Backend (FastAPI, SQLite, services) & Web Streaming Client (React, TS, Vite)

---

## Executive Summary

A comprehensive security audit of the StreamHome codebase was conducted, covering the frontend React application, backend FastAPI web server, CLI utilities, background services (FFmpeg integration, TMDB client), and installation/deployment infrastructure.

A total of **31 unique findings** were identified, categorized by severity:
- **Critical (5):** Hardcoded secrets, unencrypted credentials exposure, SSE unauthenticated access, and command/protocol injection in media services.
- **High (9):** Missing access control on core endpoints, client-side security bypasses (profile PINs), path traversal in backup/restore, and wildcard CORS.
- **Medium (11):** LocalStorage token storage, missing security headers, missing rate-limiting/brute-force protections, and lack of dependency pinning.
- **Low/Info (6):** Timezone deprecations, verbose logging, and cross-platform compatibility issues.

---

## 🔴 CRITICAL SEVERITY FINDINGS

### 1. Hardcoded Default JWT Secret & Insecure Fallback
* **Files:** [server/config.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/config.py) (Line 20)
* **Category:** Cryptography & Secret Management
* **Description:** The JWT signing key falls back to a default plaintext value (`"super-secret-key-change-me"`) if the `JWT_SECRET` environment variable is missing or empty.
* **Impact:** Any attacker with knowledge of the default key can forge valid administrative JWT tokens, bypass authentication, and compromise the application.
* **Remediation:** Remove fallback values. Raise a runtime exception at server startup if `JWT_SECRET` is not set.

### 2. Hardcoded Default API Bearer Token
* **Files:** [server/config.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/config.py) (Line 11), [web/src/App.tsx](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/src/App.tsx) (Line 32)
* **Category:** Hardcoded Credentials
* **Description:** The backend API bearer token falls back to `"secure-token-123"`, and the frontend initializes its default configuration with the same string.
* **Impact:** Allows unauthorized media ingestion, task modification, backup restoration, and administrative control.
* **Remediation:** Remove default fallbacks. Auto-generate a secure token during setup and require explicit environment configuration.

### 3. Exposure of Secrets and Credentials in Version Control
* **Files:** [server/.env](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/.env) (Line 1-3), [.gitignore](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/.gitignore)
* **Category:** Sensitive Data Exposure
* **Description:** The server `.env` containing production-level API tokens (such as `TMDB_READ_ACCESS_TOKEN`) and local keys is present in the repository, and the `.gitignore` fails to exclude the `server/.env` path.
* **Impact:** Anyone with repository access can obtain internal server tokens and TMDB credentials.
* **Remediation:** Add `server/.env` to `.gitignore`. Rotate the TMDB token and API key immediately. Purge files from git history.

### 4. Protocol & Command Abuse in FFmpeg/FFprobe Subprocesses
* **Files:** [server/services/ffmpeg.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/ffmpeg.py), [server/services/media_probe.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/media_probe.py)
* **Category:** Input Validation / Command Injection
* **Description:** User-supplied download task URLs (`video_url`, `audio_url`) are passed directly into FFmpeg and FFprobe subprocess commands. While execution uses argument lists, FFmpeg-specific protocols (e.g. `concat:`, `file:`, `subfile:`) are allowed.
* **Impact:** An attacker could craft a download URL using `file:///` or `concat:` to read arbitrary files from the filesystem and write them into processed media streams.
* **Remediation:** Restrict allowed URLs to `http://` and `https://` schemas. Use the FFmpeg `-protocol_whitelist` parameter to disallow file access protocols (`file,tcp,tls,http,https,crypto`).

### 5. SSE Download Stream Access Bypass
* **Files:** [server/routes/queue.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/queue.py) (Lines 179-190), [web/src/components/Dashboard.tsx](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/src/components/Dashboard.tsx)
* **Category:** Authentication Bypass
* **Description:** The Server-Sent Events (SSE) download progress endpoint does not require authentication because native client-side `EventSource` connections cannot send custom headers easily.
* **Impact:** Any unauthenticated observer can read real-time details of pending downloads, source URLs, files, and progress.
* **Remediation:** Secure the SSE route. Require a short-lived query token for connection authentication.

---

## 🟠 HIGH SEVERITY FINDINGS

### 6. Missing Authentication on Core API Endpoints
* **Files:** [server/main.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/main.py) (API routes), [server/routes/stream.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/stream.py)
* **Category:** Missing Access Control
* **Description:** Most user-facing endpoints (watchlist updates, playback tracking, profiles, search, settings, and media streaming) have no authentication check.
* **Impact:** Anyone with network access to the API can view user logs, watch streams, modify watchlists, and retrieve profile metadata.
* **Remediation:** Protect endpoints with authentication middleware or standard Dependency Injection checks (`Depends(get_current_user)`).

### 7. Client-Side Profile PIN Lock Bypass
* **Files:** [web/src/components/ProfileSelector.tsx](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/src/components/ProfileSelector.tsx)
* **Category:** Authorization Bypass
* **Description:** Profile PIN verification occurs entirely on the client side. Plaintext PIN values are included in API payloads and compared inside React state.
* **Impact:** Attackers can intercept network responses, read PINs, or inject React state overrides to access locked profiles.
* **Remediation:** Perform PIN checks on the server via a `/verify-pin` POST endpoint. Never send PIN strings to the frontend.

### 8. Plaintext PIN Storage and Transmission
* **Files:** [server/models.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/models.py) (Line 199), [web/src/types.ts](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/src/types.ts)
* **Category:** Cryptography
* **Description:** Profile PINs are stored in plaintext databases and sent across HTTP responses in cleartext fields.
* **Impact:** PIN values are exposed to SQL injection, database leakage, and network eavesdropping.
* **Remediation:** Hash PINs server-side using bcrypt. Exclude the PIN field from `ProfileResponse` models.

### 9. Path Traversal on Backup Restoration
* **Files:** [server/services/backup.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/backup.py) (Line 167), [server/routes/backup.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/backup.py)
* **Category:** Path Traversal
* **Description:** The database restoration endpoint joins user-supplied `filename` variables directly to backup folders using `os.path.join` without path traversal validations.
* **Impact:** Authorized users could rewrite critical database configurations by supplying traversal paths pointing to arbitrary local sqlite structures.
* **Remediation:** Sanitize filenames with `os.path.basename` and verify the canonical path remains within the backup root folder.

### 10. Broad Wildcard CORS Configuration with Credentials Allowed
* **Files:** [server/main.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/main.py) (Lines 190-196)
* **Category:** CORS Configuration
* **Description:** CORS is configured to allow wildcard origins (`*`) while also enabling `allow_credentials=True`.
* **Impact:** Third-party websites could query the backend directly using browser contexts.
* **Remediation:** Enforce specific origin allowlists and disable wildcard credential headers.

### 11. Public Network Bindings (0.0.0.0) in Production Contexts
* **Files:** [server/main.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/main.py), [web/server.ts](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/server.ts), start scripts
* **Category:** Network Security
* **Description:** The FastAPI and Express environments bind by default to `0.0.0.0`, exposing services to the whole subnet.
* **Impact:** Exposes unauthenticated services directly to public interfaces.
* **Remediation:** Bind to `127.0.0.1` by default, using a reverse proxy (e.g. nginx or Caddy) with TLS for external connections.

### 12. Command Argument Injection via Settings Mutations
* **Files:** [server/cli.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/cli.py) (Line 144), [server/main.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/main.py)
* **Category:** Input Injection
* **Description:** Settings modification routes use generic `setattr` mappings for environment values, allowing external configuration of paths (like `RCLONE_REMOTE_PATH`).
* **Impact:** Attackers could inject customized rclone command switches into parameters later invoked by subprocess routines.
* **Remediation:** Enforce validation patterns and allowlists on mutable configuration keys.

### 13. Missing Download Integrity Checks on Dependency Setup
* **Files:** [setup.bat](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/setup.bat), [setup.sh](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/setup.sh)
* **Category:** Supply Chain Risk
* **Description:** Binaries for FFmpeg, Node, Python, and Rclone are downloaded and installed silently without checksum validation.
* **Impact:** Man-in-the-middle attacks or compromised mirror servers could inject trojanized installers.
* **Remediation:** Pin exact versions and verify SHA-256 hashes of download payloads.

### 14. Global Modal Vulnerability to Code Injection
* **Files:** [web/src/components/Dashboard.tsx](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/src/components/Dashboard.tsx) (Lines 754-762)
* **Category:** DOM Security
* **Description:** The dashboard binds an unvalidated JSON handler directly to `window.updateDetailsModal`.
* **Impact:** Script code executed via extension or XSS could spoof data and inject content into active UI layouts.
* **Remediation:** Utilize React contexts or secure message passing APIs instead of global DOM bindings.

---

## 🟡 MEDIUM SEVERITY FINDINGS

### 15. Insecure Access Token Storage in LocalStorage
* **Files:** [web/src/App.tsx](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/src/App.tsx)
* **Category:** Session Management
* **Description:** Active JWT sessions and API tokens are saved in `localStorage`.
* **Impact:** Any XSS vulnerability could immediately access and exfiltrate credentials.
* **Remediation:** Shift token storage to `httpOnly`, `Secure`, `SameSite=Strict` cookies.

### 16. Lack of CSRF Protection
* **Files:** Entire project (Routes & Client)
* **Category:** Session Security
* **Description:** Web forms and POST actions lack CSRF tokens.
* **Impact:** Cross-site scripts can trigger actions on behalf of authenticated browser sessions.
* **Remediation:** Implement standard CSRF verification.

### 17. 2FA Verification Password Bypass
* **Files:** [server/routes/auth.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/auth.py)
* **Category:** Access Control
* **Description:** The `/verify` route issues a JWT token solely based on username and TOTP input, without checking if password verification succeeded.
* **Impact:** An attacker who obtains only the TOTP secret can authenticate without knowing the user's password.
* **Remediation:** Issue a short-lived "pre-auth" token during `/login` that must be presented to `/verify`.

### 18. Absence of Rate Limiting
* **Files:** Authentication routes
* **Category:** Brute Force Protection
* **Description:** Login and TOTP routes lack IP-based rate limiting.
* **Impact:** Attackers can perform brute-force guessing against user passwords or 2FA secrets.
* **Remediation:** Implement rate limits using middleware (e.g. `slowapi`).

### 19. Plaintext TOTP Secret Storage
* **Files:** [server/models.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/models.py) (Line 132)
* **Category:** Data Protection
* **Description:** Database fields store TOTP provisioning secrets in plaintext.
* **Impact:** Database leakage compromises 2FA credentials for all accounts.
* **Remediation:** Encrypt TOTP secrets at rest.

### 20. Exposed Transcode Cache Paths
* **Files:** [server/routes/stream.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/stream.py) (Lines 25-26, 165)
* **Category:** Path Traversal
* **Description:** Unvalidated `quality` parameter strings are interpolated directly into cache file paths.
* **Impact:** Crafting directory traversal sequences in quality settings could access files outside the cache directory.
* **Remediation:** Restrict the parameter to an allowlist (e.g. `"720p"`, `"480p"`, `"Source"`).

### 21. Sensitive Headers Logged in FFmpeg Executions
* **Files:** [server/services/ffmpeg.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/ffmpeg.py)
* **Category:** Log Exposure
* **Description:** Outbound headers (including HTTP Authorization tokens) are printed directly to log buffers.
* **Impact:** Exposes authentication credentials to system log files.
* **Remediation:** Redact authorization elements from logs.

### 22. Insecure Log Directories
* **Files:** [server/services/logger.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/logger.py) (Line 8)
* **Category:** Info Leakage
* **Description:** Logs are written to public temp folders, potentially accessible via static mounts.
* **Impact:** System logs could be exposed over HTTP.
* **Remediation:** Save log targets in protected folders (e.g. `/var/log` or a dedicated restricted directory).

### 23. Auto-Updates Executing Unchecked Git Commands
* **Files:** [server/services/update.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/update.py)
* **Category:** Supply Chain Risk
* **Description:** Remote repository pulls execute package installation scripts automatically on receipt.
* **Impact:** Compromises in remote repositories can trigger malicious remote code execution.
* **Remediation:** Enforce code signing validation and manual confirmation before update execution.

### 24. Unpinned Backend Dependencies
* **Files:** [server/requirements.txt](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/requirements.txt)
* **Category:** Build Security
* **Description:** Python library requirements specify minimum versions (`>=`) instead of pinned values.
* **Impact:** Future package updates could introduce security regressions.
* **Remediation:** Pin absolute requirements using lockfiles.

### 25. Exposed Source Maps in Builds
* **Files:** [web/package.json](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/web/package.json) (Line 8)
* **Category:** Info Leakage
* **Description:** Client bundles build with active source maps.
* **Impact:** Allows reconstruction of client-side TypeScript source structures.
* **Remediation:** Disable source maps in release distributions.

---

## 🟢 LOW & INFO SEVERITY FINDINGS

### 26. Deprecated Timezone API Calls
* **Files:** [server/routes/auth.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/auth.py)
* **Category:** Code Quality
* **Description:** Uses naive timezone methods (`datetime.utcnow()`).
* **Impact:** Timezone calculation discrepancy in token validations.
* **Remediation:** Use standard UTC aware declarations (`datetime.now(timezone.utc)`).

### 27. Broad Exception Catching in JWT Checks
* **Files:** [server/routes/auth.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/routes/auth.py) (Line 54)
* **Category:** Error Handling
* **Description:** Catches global exceptions, masking internal system failures as simple validation errors.
* **Impact:** Diagnostic obfuscation.
* **Remediation:** Catch specific JWT errors, leaving internal bugs to emit HTTP 500 codes.

### 28. Overly Broad Process Terminations
* **Files:** [stop.bat](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/stop.bat), [stop.sh](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/stop.sh)
* **Category:** Operation Risk
* **Description:** Shutdown routines terminate all python and node instances globally.
* **Impact:** Terminates unrelated services running on the same host.
* **Remediation:** Store process IDs in `.pid` containers and close target processes explicitly.

### 29. Plaintext Secrets Displayed on Terminals
* **Files:** [server/cli.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/cli.py)
* **Category:** Secret Management
* **Description:** CLI outputs OTP keys and authorization tokens directly to terminals.
* **Impact:** Credentials can leak via terminal command logs or screen sessions.
* **Remediation:** Provide QR code displays only and clear outputs immediately after use.

### 30. Unvalidated File Sizes in Download Managers
* **Files:** [server/services/queue.py](file:///c:/Users/deniz/Desktop/.all/Projects/The%20Project/server/services/queue.py)
* **Category:** Resource Safety
* **Description:** Media asset retrievals lack maximum file size validations.
* **Impact:** Large file targets could exhaust local filesystem spaces.
* **Remediation:** Enforce content size limits before writing incoming streams.

### 31. Windows-Only Imports in Admin Scripts
* **Files:** [server/cli.py](file:///c:/Users/deniz/Desktop/.all/Projects/TThe%20Project/server/cli.py)
* **Category:** Portability
* **Description:** CLI uses `msvcrt` imports unconditionally.
* **Impact:** Admin scripts will crash if deployed on non-Windows servers.
* **Remediation:** Add platform condition guards for OS specific imports.
