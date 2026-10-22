# Management authority and journals

Lifecycle authority version 2 adds strict management journals and operation-bound
persistence. Managed configuration activation, systemd control, environment
preparation and [reviewed forward recovery](management-recovery.md) use these
contracts. Legacy startup, update and restore retain their existing format;
their managed adapters remain a separate integration requirement.

## Authority versions

Missing authority still reads as version 1, idle, generation 0 without creating
state. Ordinary claims never upgrade or downgrade the installed format. All
claim, transition, owner-transfer, completion and controller-lease writes retain
the inspected version.

| Authority format | Operations and ownership |
| --- | --- |
| 1 | Existing update/restore representation; management enum values are rejected |
| 2 | `update`/`updater`, `restore`/`restore_controller`, `management`/`management_controller`; other pairs are rejected |

All three operations use the same state lock, exclusion, monotonic generation,
phase transitions, capability validation and controller lock. Ownership transfer
rotates the capability and increments generation. Management cannot reserve an
operation over an unresolved update or restore. Public projections omit secrets.

There is deliberately no production migration entry point yet. Provisioning or
adoption must qualify both target and rollback readers and perform its explicit
idle-only migration. Tests initialize disposable version-2 fixtures directly;
those writes are not a migration procedure. Older version-1-only binaries reject
version 2; reverting a source checkout cannot downgrade authority.

Until guarded management startup and recovery are complete, bootstrap returns
`management_startup_unavailable` with no runtime effects. The legacy controller
CLI cannot claim, transition or complete management. Its recovery planner and
cleanup reject management instead of falling through to update behavior. These
guards must remain until the corresponding complete protocols replace them.

## Journal version 1

`server/lifecycle/management_journal.py` validates immutable subordinate records
without filesystem I/O or application imports. The complete top-level fields are:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer 1 |
| `installation_id`, `operation_id`, `review_id` | Canonical nonzero UUIDs |
| `generation` | Positive signed-64-bit integer, never a boolean |
| `action` | `configure_apply`, `repair_environment`, `provision_control` |
| `activation_mode` | `cutover`; `save_only` only for configure; `os_only` only for control provisioning |
| `previous`, `candidate` | Complete execution tuples below |
| `initial_service_state` | `running` or `stopped` |
| `controller_invocation_id` | Nonzero 32-character lowercase hexadecimal invocation identity |
| `supervisor_invocation_id` | Same representation when initially running; null when initially stopped |
| `stage` | A mode-compatible stage from the table below |
| `acceptance` | Null, or exact candidate-tuple and verification-evidence SHA-256 hashes |

An execution tuple contains all of these fields:

```text
application_commit, application_build_id, deployment_revision,
runtime_bundle_id, runtime_manifest_sha256,
python_environment_id, python_executable,
node_environment_id, node_executable,
web_bundle_id, web_dependency_target, web_dist_target,
web_asset_manifest_sha256,
control_bundle_id, control_manifest_sha256, control_protocol_version,
policy_revisions, provider_revisions
```

The application commit is a complete lowercase 40-character Git object ID.
Build IDs use the existing bounded alphanumeric/build punctuation convention.
Revision and bundle/environment IDs are canonical nonzero UUIDs; artifact hashes
are lowercase 64-character SHA-256 strings. Control protocol version 1 is the only
recognized value. Domain-revision objects contain at most 16 bounded lowercase
domain names with nonnegative signed-64-bit integer revisions. Empty objects mean
no domain binding was required; they do not establish a domain's default policy.
These references do not transfer ownership from policy or provider services.

Executable paths must be exactly
`/opt/streamhome/runtime/python/<python_environment_id>/venv/bin/python` and
`/opt/streamhome/runtime/node/<node_environment_id>/bin/node`. Web dependency and
dist targets must both use `/opt/streamhome/runtime/web/<web_bundle_id>/` with
the exact `node_modules` and `dist` suffixes. Parsing checks representation and
binding; artifact existence, provenance, manifests, symlink safety, compatibility
and actual serving identity still require preflight and runtime verification.

Configure can change only the deployment revision. Environment repair preserves
source, build, deployment, assets, control and domain revisions while replacing
the bound environment identities. Control provisioning changes only its control
identity; OS-only completion retains the entire execution tuple. None of these
actions can encode database restoration.

| Mode | Stages |
| --- | --- |
| All | `reserved`, `prepared`, `recovery_required` |
| Cutover | Also `exclusive`, `verifying`, `accepted`, `succeeded`, `rolled_back` |
| Save only | Also `saved`; no candidate runtime acceptance |
| OS only | Also `os_completed`; no candidate runtime acceptance |

Acceptance contains exactly `execution_tuple_sha256` and `verification_sha256`.
The first must match canonical compact, sorted-key JSON of the candidate tuple.
Accepted/succeeded stages require it. Acceptance can survive recovery-required
but cannot disappear or change, and cannot coexist with rollback. The second
hash identifies evidence; this validator does not prove that evidence is valid.
Ordinary progress cannot skip stages, relabel reviewed inputs or implicitly
resume recovery. Recovery and generation rebinding need their own exact protocol.

The JSON input bound is 32 KiB. Duplicate, missing and unknown fields, nonstandard
numbers, malformed UTF-8 and unsupported versions/action-mode pairs fail closed.
An authority comparison additionally requires exact version, management type,
owner, operation ID, generation and phase. A comparison of snapshots grants no
permission to mutate or launch anything.

## Canonical-bound persistence

`server/lifecycle/management_store.py` stores `management/<operation-id>/journal.json`
under the canonical lifecycle directory. Managed installations resolve it to
`/var/lib/streamhome/lifecycle`; legacy checkouts retain their existing resolver.
The UID management controller uses this store for ordinary operation progress.
Explicit generation takeover uses a separate retained-intent protocol and cannot
be performed through an ordinary journal update.

Creation and progress require an exact living controller lease and current
credential under controller-lock-before-state-lock ordering. The journal must
match canonical MANAGEMENT authority. Creation permits only `reserved`; progress
uses an exact prior-record CAS and the canonical phase corresponding to the new
stage. The store neither transitions nor completes authority. A journal claiming
acceptance cannot independently accept a runtime or reopen admission.

Parents must be private owned directories. Reads reject symlinks, nonregular
files, extra hard links, unsafe ownership/permissions and detected replacement;
FIFO inspection does not block. Inspection creates no directories or locks and
can inspect retained records after authority completion. Publication uses the
existing atomic file-replace/file-fsync/directory-fsync machinery. Exact retries
reaffirm directory durability before acknowledgement. Persistence uncertainty
retains authority and evidence; it cannot acknowledge success or force-clear.

## Remaining integration gates

Guarded verification/acceptance, settled admission, protected provisioning,
configuration activation and same-supervisor forward recovery are implemented
for the qualified managed runtime. Guarded rollback/safe abort, stopped/replaced
supervisor recovery, bound environment/control reconstruction and managed
update/restore integration remain separate incomplete gates. Existing schema
validators or link-recovery primitives do not make those operations available.
