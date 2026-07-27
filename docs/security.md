# Security

StreamHome is a self-hosted application. The operator is responsible for the host, TLS termination, firewall, updates, backups, and account recovery.

## Alpha security controls

- Passwords and profile PINs are bcrypt hashes.
- Profile PIN hashes are never returned by the API.
- Two-factor authentication uses local TOTP only. SMTP and email OTP are not supported.
- Authentication uses HttpOnly session cookies.
- Sensitive account, update, backup, and storage mutations require recent reauthentication.
- Ingestion uses scoped integration credentials rather than the administrator session.
- Rclone configuration is application-owned and encrypted.
- Setup is protected by a one-time bootstrap code and a short-lived signed session.

## Network deployment

Expose only the web port through a trusted HTTPS reverse proxy. Keep API port 8000 bound to loopback. Configure `PUBLIC_URL`, allowed origins, and trusted proxy CIDRs explicitly.

## Profile PIN scope

A profile PIN prevents selection through the normal StreamHome client. It is not a replacement for the administrator account, filesystem permissions, or network access control.

## Dependency exception

The alpha uses the latest available React Router 7 release. The npm advisory database currently reports high-severity findings in React Router’s React Server Components action mode. StreamHome is a client-rendered SPA and does not enable that mode, but the advisory remains visible until an installable upstream fixed release is available. Recheck `npm audit` before every release.

Report security problems privately to the repository owner rather than opening a public issue containing exploit details or secrets.
