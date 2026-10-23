# Managed operation recovery

Managed installations retain the original operation when configuration activation
or environment activation is interrupted. A failed command does not authorize a
reinstall, erase lifecycle state or reverse database writes. Recovery continues
the recorded application, configuration and runtime tuple after a fresh review.

An originally running CUTOVER path can continue through its original live systemd
supervisor, or use a separately reviewed start after the whole service has stopped.
It supports failure before configuration publication,
failure during guarded verification, failure after acceptance and a terminal
`succeeded` journal whose canonical completion was interrupted. Preserving
SAVE_ONLY and OS_ONLY operations can also continue with their original running
or stopped outcome. A running outcome requires the identical settled backend and
supervisor; it never restarts either process. A stopped outcome never starts them.
An originally stopped environment repair instead verifies its inactive artifacts
and retains the stopped outcome. A replacement running supervisor cannot borrow
the original invocation. Stop the owned service before requesting the fresh start
review below. Operation-bound environment/control reconstruction remains a
separate maintenance protocol.

## Repair with the service stopped

`sudo streamhome repair` can rebuild the accepted application's private Python,
Node.js and web dependency generations while `streamhome.service` is stopped.
It uses the authenticated independent control reader and base OS Python, so a
broken selected application Python or Node environment does not prevent repair.
The installation must have idle, resolved authority and no foreign service-account
work. The application commit, deployment, policy/provider revisions and database
are retained.

Preparation runs in a cancellable root issuer scope, without claiming MANAGEMENT
or changing the active execution. Activation claims MANAGEMENT only after the
complete candidate tuple is known. It verifies artifacts, paired runtime links
and the stopped process set before accepting and publishing that tuple. It does
not start the application or claim live readiness. Run `sudo streamhome start`
separately when the application should run; normal startup recovery still applies.

An interrupted activation uses the same exact-operation `recover` review below.
Both preparation and stopped recovery permissions are bound to the current issuer
scope, boot and stop intent. Stopping the already inactive runtime cancels those
permissions. Historical evidence remains readable after the issuer exits; it
cannot authorize a new attempt. Cleanup follows the latest recovery grant and
cannot stop a controller adopted by a newer explicit owner.

## Repair an unhealthy running service

`sudo streamhome repair` also supports a living supervisor whose backend cannot
acknowledge normal admission. It prepares new private environments with the
independent control reader and base OS Python. It binds the exact supervisor and
a cancellable root scope; it does not borrow a backend cache-preparation or drain
acknowledgement. Other service-account work outside the owned runtime and exact
preparation unit prevents this path.

The complete candidate is verified before reserving MANAGEMENT. After activation
approval, the root bridge records one whole-service stop intent and stops only
the exact owned unit. Stop propagation cancels that issuer and controller. The
command reports the operation UUID and the fresh `recover --operation UUID`
review needed to start the guarded replacement. This continuation-required result
is not a completed repair or permission to restart automatically. Declining that
new review leaves the service stopped and the original operation unresolved.

Broken selected Python or Node files do not block independent preparation. The
old execution remains selected until the replacement passes guarded acceptance
and mandatory recovery. Application commit, configuration, policy/provider
revisions, assets and database writes are preserved. A stop during preparation
cancels the builder before any claim; it never silently resumes the service.

## Review and continue

Use the exact unresolved operation UUID reported by installation diagnostics:

```bash
sudo streamhome recover --operation <operation-uuid> --review
sudo streamhome recover --operation <operation-uuid>
```

The first command prints current evidence without changing canonical authority,
configuration or application processes. The second reviews the recorded recovery
action. An originally running CUTOVER interrupts owned application children; SAVE_ONLY and OS_ONLY
preserve them. Empty Enter at its `[Y/n]` prompt means Yes; EOF,
missing terminal and interrupted input are not consent. A declined review leaves
the original operation unresolved.

Automation uses `--request FILE` instead of interactive input. The JSON must contain
exactly `schema_version` (1), `operation_id`, `expected_generation`,
`expected_evidence_sha256`, `continue_recovery` and `interruption_approved`.
Obtain the generation and evidence hash from a fresh `--review`. Both consent
fields must be JSON booleans and true for continuation. Stale evidence is rejected;
there is no force option or implicit retry loop.

## Resume an originally running cutover after a stop

When the recorded CUTOVER operation was running but the service is now stopped,
the same `recover` command presents a fresh start review inside a new cancellable
root scope. This review explicitly authorizes one guarded start of the selected
recorded runtime. Enter means Yes; declining leaves the service stopped. A review
printed by `--review` alone does not authorize that start.

Automation must answer the new live challenge. Start `recover --request FILE`
with a nonexistent response path. It prints `resumption_review`, its SHA-256 and
`request_schema_version: 3`, then waits up to 900 seconds. Atomically publish a
JSON response containing exactly these fields:

- `schema_version`: 3.
- `operation_id`, `expected_generation`, `expected_evidence_sha256`: the current
  operation, generation and canonical evidence from the emitted review.
- `action`: `continue_recovery`, `rollback` or `safe_abort`, matching the command.
- `continue_recovery`, `interruption_approved`, `resume_service`: JSON booleans,
  all true to approve.
- `expected_resumption_sha256`: the newly emitted `resumption_review_sha256`.

Each challenge binds the living issuer, boot, stop intent, selected execution and
generation. Schema-1/2 requests and responses from previous scopes cannot approve
a new start. A stop cancels pending permission even while the runtime is inactive.
Consumption is durable before the fixed systemd start; ExecStartPre binds its one
new invocation. A timeout or lost reply never retries that consumed permission.
If the outcome remains unresolved, obtain a fresh review after the service is
stopped. No automatic fallback to a different runtime is authorized.

The independent UID observer verifies canonical ownership and paired artifacts
before launch. The replacement supervisor starts the exact guarded runtime,
retains the original supervisor identity in journal history and requires live
root custody until exact accepted completion. The temporary binding can then be
released in that same process. A stored ticket cannot enroll another supervisor.
The bound control reader must explicitly support recovery requests 1/2/3 and
resumption plan 1; older readers are refused for this path.

Before candidate acceptance, an evidence-supported review may offer guarded
restoration of the previous runtime:

```bash
sudo streamhome recover --operation <operation-uuid> --rollback --review
sudo streamhome recover --operation <operation-uuid> --rollback
```

`--abort` selects that same guarded previous-runtime procedure only while the
original unaccepted reservation has not entered exclusive cutover. Both choices
interrupt owned children for an originally running operation and require compatible bound control readers. An originally stopped repair keeps the service stopped. Neither
restores the user database. The failed configure revision is quarantined; a
revision never published as desired remains unpublished. An explicit subsequent
configure retry gets a new revision, rather than removing the old quarantine.

Automation for rollback/abort uses request schema 2: all schema-1 fields plus
exactly `action`, set to `rollback` or `safe_abort` to match the CLI option. A
schema-1 forward consent cannot authorize either action. The requested action
must still appear in a fresh review's permitted actions.

The previous runtime must pass guarded startup, durable acceptance and mandatory
recovery before completion. Once it is accepted, a failed recovery can only
continue that same previous tuple with its original acceptance. The journal keeps
both original tuples and an explicit previous-runtime target; it never changes
which tuple was originally reviewed as the candidate.

Preserving recovery completes the originally reviewed boot preference with a new
finite OS grant. Forward CUTOVER recovery preserves the current boot preference;
rollback/abort reviews restoration of the original preceding boot preference. No older
OS receipt authorizes either action. A preserving backend acknowledges the full
same-operation takeover chain from its process-local settled generation. Terminal
save/OS evidence is durable before authority release; an interrupted release can
be reviewed again. Disk receipts cannot admit a replacement backend.

## Ownership and durable evidence

Root runs only the authenticated protected control entry. Application state and
journals are interpreted after dropping to the dedicated service UID. A root grant
binds the original request, exact operation/generation, fresh review, boot identity,
stop epoch and a finite monotonic lifetime. Root OS effects require a separate
receipt from that exact living UID controller; an old receipt cannot grant a new
generation authority.

Under controller-lock exclusion, recovery rechecks the evidence and persists its
intent before rotating the generation and owner capability. The source journal,
unique review and per-generation takeover record remain retained. A subsequent
review can explain a crash between transfer and journal rebinding. Missing or
unexplained lineage is an error, rather than permission to infer a new installation.

Configuration receipts are copied into the new generation with the same reviewed
base and target. If the original operation never published a receipt, recovery may
create one only while active and desired configuration still match the reviewed
base. An already changed manifest cannot manufacture evidence for a missing receipt.

Web dependency and build links are reconstructed only from the original bound
previous/candidate pair and retained receipts, after proving children stopped.
Referenced environments and recovery evidence remain available. No database,
media, account or provider rollback is performed.

After a completed previous-runtime recovery, a later operation can reuse the
retained original binding receipt only by proving the accepted execution, exact
terminal journal and completed paired-link receipt naming that original digest.
Repeated early aborts may retain the same older base receipt. Missing, incomplete
or changed checkpoint evidence remains a conflict; the original receipt is not
rewritten to make a new operation appear explainable.

## Acceptance and completion

An unaccepted selected runtime must pass guarded verification before acceptance. Existing
acceptance is preserved byte for byte: recovery cannot relabel another runtime as
accepted or switch to another tuple after acceptance.

The accepted backend performs mandatory recovery before the controller publishes
its terminal journal and completes canonical authority. The application separately
observes that exact terminal journal and idle generation before opening ordinary
traffic. Observing completion between two checks must not be treated as a lifecycle
failure. A different generation or missing terminal evidence still closes admission.

The qualified native environment is Ubuntu 24.04, x86_64, systemd/cgroup v2 and
Python 3.12.3. Source tests and disposable native fault injection do not certify
valuable-data recovery, other distributions or physical power-loss behavior.
