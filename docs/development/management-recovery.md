# Managed operation recovery

Managed installations retain the original operation when configuration activation
or environment activation is interrupted. A failed command does not authorize a
reinstall, erase lifecycle state or reverse database writes. Recovery continues
the recorded application, configuration and runtime tuple after a fresh review.

The CUTOVER path requires the original systemd supervisor to remain running.
It supports failure before configuration publication,
failure during guarded verification, failure after acceptance and a terminal
`succeeded` journal whose canonical completion was interrupted. Preserving
SAVE_ONLY and OS_ONLY operations can also continue with their original running
or stopped outcome. A running outcome requires the identical settled backend and
supervisor; it never restarts either process. A stopped outcome never starts them.
Stopped or replaced CUTOVER supervisors and operation-bound environment/control
reconstruction require separate maintenance protocols.

## Review and continue

Use the exact unresolved operation UUID reported by installation diagnostics:

```bash
sudo streamhome recover --operation <operation-uuid> --review
sudo streamhome recover --operation <operation-uuid>
```

The first command prints current evidence without changing canonical authority,
configuration or application processes. The second reviews the recorded recovery
action. CUTOVER interrupts owned application children; SAVE_ONLY and OS_ONLY
preserve them. Empty Enter at its `[Y/n]` prompt means Yes; EOF,
missing terminal and interrupted input are not consent. A declined review leaves
the original operation unresolved.

Automation uses `--request FILE` instead of interactive input. The JSON must contain
exactly `schema_version` (1), `operation_id`, `expected_generation`,
`expected_evidence_sha256`, `continue_recovery` and `interruption_approved`.
Obtain the generation and evidence hash from a fresh `--review`. Both consent
fields must be JSON booleans and true for continuation. Stale evidence is rejected;
there is no force option or implicit retry loop.

Before candidate acceptance, an evidence-supported review may offer guarded
restoration of the previous runtime:

```bash
sudo streamhome recover --operation <operation-uuid> --rollback --review
sudo streamhome recover --operation <operation-uuid> --rollback
```

`--abort` selects that same guarded previous-runtime procedure only while the
original unaccepted reservation has not entered exclusive cutover. Both choices
interrupt owned children and require compatible bound control readers. Neither
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
