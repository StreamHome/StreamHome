# Managed configuration and installer components

These stdlib-only components implement deployment validation, installer/configure
drafts, subordinate revision publication, listener preflight and isolated update
discovery. They do not yet replace `install.sh`, register a service, publish a
managed descriptor or provide the global `streamhome` command. Their production
entry points still require the complete guarded management, settlement, protected
provisioning and shared-path protocols described in the
[management contract](management-protocol.md).

## Deployment snapshot

`server/lifecycle/deployment_config.py` owns the six supported deployment fields:

| Field | Meaning |
| --- | --- |
| `access_mode` | `local`, `lan`, `reverse_proxy` or `cloudflare` |
| `web_port` | ASCII decimal 1024–65535, excluding backend port 8000 |
| `public_origin` | One canonical HTTP/HTTPS origin, without credentials, path, query or fragment |
| `trusted_proxy_cidrs` | Explicit immediate proxy networks; universal trust is rejected |
| `extra_allowed_origins` | At most eight additional exact canonical origins |
| `cloudflare_connector_id` | Existing protected connector UUID, only in Cloudflare mode; never a token |

The default is local access at `http://localhost:3000`. Direct local access must
match the IPv4 loopback listener; direct LAN access uses an IPv4 wildcard web
listener. IPv6 public origins are supported behind a proxy, but direct IPv6
listeners are outside this initial contract. Reverse proxy and Cloudflare modes
keep the web listener on IPv4 loopback and require an HTTPS origin. The backend
binding is not a deployment override. Cloudflare mode requires a routed DNS name.

Allowed origins always include the public origin. HTTPS-cookie policy, public
URL and proxy settings derive from the same snapshot. Origins reject control
characters, credentials, ambiguous numeric hosts, wildcard DNS, malformed ports
and noncanonical DNS labels; ASCII/punycode DNS is supported. Unknown deployment
fields, duplicate JSON fields and nonstandard numbers are rejected.

`startup_environment()` produces revision-bound compatibility data only. No
current startup consumer uses it yet. It must never become a second writable
configuration store or shell input sourced by a privileged process.

## Terminal and draft behavior

`terminal.py` explicitly opens separate read/write `/dev/tty` streams. It never
reads a pipe-fed shell script as answers. Yes/no prompts show `[Y/n]`, default to
Yes on Enter and reprompt for invalid answers. EOF, Ctrl-C, overlong lines and
terminal-control input cancel. Ordinary empty fields retain the displayed value.
Configuration text is escaped for terminal control characters. Progress stages
show done/failed completion; redirected output contains plain lines.

`configure_wizard.py` prepares immutable drafts without writing files or touching
services. The installer asks about port, access and boot intent, with fixed
installation paths. Configure offers port, access/public origin, exact additional
origins, trusted proxies, boot intent and caller-supplied read-only diagnostics.
Back abandons an edit; cancel and no-op return no mutation request. Where saved
and active deployments differ, both are shown and the saved endpoint is explicitly
pending. Cloudflare selection accepts protected references supplied by the caller
and explains the separate provider routing step.

Review displays before/after values and asks separately about application and
controlled activation. Declining restart returns a save-only request and does
not claim the new endpoint is live. Port and complete-snapshot diagnostics run
before approval; lifecycle admission, fresh CAS, effects and actual readiness
remain the eventual controller's responsibility.

Noninteractive parsers require complete explicit installation settings and
`install=true`, or a configuration patch bound to an exact expected desired UUID,
explicit boolean boot/apply/activation choices and schema version 1. They reject
missing/unknown fields, coercive booleans, duplicate JSON and oversized input.
They return requests only; parsing is not permission to perform effects.

## Configuration manifest and apply receipts

`configuration_store.py` accepts an already protected, service-owned configuration
directory FD. The future protected entry point must resolve trusted ancestors.
There is no path/environment override, owner override or automatic initialization
when the manifest is missing. Initial publication belongs to protected first-install
provisioning. This store is not wired to legacy `.env`/`settings.json` writers.

An immutable deployment document binds schema version 1, installation UUID,
revision UUID and the complete deployment snapshot. The manifest independently
references active and desired deployments by UUID and canonical SHA-256, retains
failed candidate UUIDs in quarantine and carries separate policy/provider revision
maps. Those maps acknowledge independently admitted publishers; they do not apply
policy or replace provider-owned publication. Unknown version/shape, unsafe file
ownership, extra hard links, symlinks, FIFOs and detected replacement fail closed.

Inactive revision preparation never changes desired/active state. Desired
publication requires the exact persisted prepared `configure_apply` journal,
current MANAGEMENT credential and living controller lease, plus the expected
desired revision, unchanged active identity and matching policy/provider bindings.
Lock order is controller, lifecycle state, then configuration writer. A receipt
binds the operation/generation, immutable journal identity, expected desired,
previous active and target digests before manifest replacement. Save-only records
`saved`; cutover records `applying`. Exact retries reconcile lost durable replies;
they cannot substitute new reviewed inputs.

Only an exact durably accepted configure candidate can become active. Acceptance
receipt reconciliation proceeds forward if the manifest changed before its reply
was lost. Proven pre-acceptance failure can quarantine the desired candidate and
record `failed`, retaining the old active reference. None of these paths opens,
copies or restores a user database. Receipts are subordinate evidence: they cannot
accept a runtime, grant launch permission, settle admission or release authority.

Independent domain-reference CAS preserves pending deployment references. It
requires idle v2 authority at the expected generation and an increasing domain
revision. Policy/provider implementations must validate and accept their own
artifacts before publishing a reference through this interface.

## Listener preflight and existing installations

`port_check.py` probes the selected IPv4 binding without REUSEADDR/REUSEPORT and
closes its temporary socket. A conflict never kills another process. Reusing an
occupied unchanged port additionally requires the exact installation UID, PID and
start identity, systemd service cgroup, supervisor invocation, service marker,
network namespace and ownership of every overlapping listening socket. Evidence
is rechecked, including IPv6 wildcard/IPv4-mapped overlap. Successful preflight
does not reserve the port; activation must recheck and prove real bound readiness.

`install_discovery.py` reads candidate ownership, official origin, full commit,
expected markers and private-checkout exclusions as bounded data. It never
executes candidate code, Git hooks or `.env`. It supports loose/packed main refs
and detached commits; known private development checkouts are unavailable for
production adoption. Original-user home discovery uses passwd/sudo identity,
not an arbitrary HOME. Protected descriptor/effective unit metadata must be
supplied by their trusted reader. Multiple, partial, inaccessible, conflicting or
explicitly missing locations block a second installation. This helper does not
yet collect effective systemd metadata or implement legacy adoption.

`release_probe.py` checks a fixed official HTTPS repository in one private
disposable bare Git repository. It never fetches into installed Git, rewrites a
remote, writes lifecycle state, clears failed targets or updates the application.
It distinguishes identical, verified eligible, different/unverified, ahead,
diverged, policy rejection, unavailable and stale/retry results. Changed installed
evidence or release policy invalidates the result. Signature-required policy
remains unverified without a qualified separately bound signer adapter; it is
never silently disabled. Signer adapter integration and persisted policy readers
remain pending. `command_runner.py` bounds command time and total output and
terminates only its own unreaped command group on interruption/failure.

These helpers are source-tested with disposable files, real sockets and temporary
Git histories. They do not establish native systemd installation, proxy sign-in,
provider routing, first-install resumption or runtime cutover qualification.
