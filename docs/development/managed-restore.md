# Managed restore custody and response confirmation

Managed restore keeps the existing canonical restore operation, checkpoint,
database activation, guarded verification, acceptance and rollback engine. It
does not start a second MANAGEMENT operation or run detached application children.

## Original execution binding

Restore journal version 2 retains an immutable `managed_execution` record: the
installation and operation identities, original generation, complete execution
tuple, supervisor PID/start/invocation, boot identity and stop epoch. Capture
requires the canonical RESERVED owner and the exact supervised backend. Legacy
version-1 journals do not acquire a binding inferred from current active links.
Ordinary progress and generation transfer preserve the original record.

Support contract version 6 explicitly declares descriptor handoff version 1 and
managed restore version 1. Reader negotiation precedes journal publication. This
declaration does not advertise managed update or operator recovery support.

## Finite descriptor transfer

The owned supervisor accepts one bounded AF_UNIX SOCK_SEQPACKET offer from its
exact backend. It retains the three distinct pipe ends and starts only the fixed
`streamhome-operation@<operation>.service` unit through the existing narrow polkit
rule. The exact operation controller collects them with kernel peer credentials,
current canonical ownership and fresh PID/cgroup/unit checks.

SCM_RIGHTS carries the capability input, ownership acknowledgement output and
response-confirmation input as separate descriptors. Capabilities never enter
metadata, arguments, environment or persistent transport files. Receivers enforce
pipe direction, distinct inode identity, close-on-exec and strict ancillary data.
Consumption precedes transfer. Expiry, disconnect, failed unit start, replay and
changed ownership close custody without issuing an acknowledgement or launch
permission. Shutdown releases pending descriptors and connection tasks.

The controller rotates canonical generation/capability under the existing lease,
persists the parent journal and sends the real ownership acknowledgement. It
cannot begin destructive work until its separate response pipe receives `1`.
EOF or timeout preserves recovery-required authority and the original database.

## Actual HTTP boundary

`ControllerResponseGateMiddleware` is a pure ASGI wrapper outside the application's
buffering middleware. Its behavior is restricted to restore POST requests.
`ControllerResponse` transfers pipe custody to that wrapper. The pipe opens only
after the actual outer final HTTP body send completes with status 202 and without
an observed disconnect. A buffered inner send or background callback cannot
confirm delivery. Missing middleware, cancellation, send failure or replacement
with another status closes the pipe without authorization.

Disconnect observation serializes access to the ASGI receive channel, including
other middleware listeners. Normal server completion may itself produce
`http.disconnect`; completion is marked immediately after the final send so that
event is not mistaken for an earlier failed delivery. ASGI send completion is not
a physical network acknowledgement from the client.

## Supervisor-owned transitions

Authenticated controller RPC stops owned application children, starts maintenance,
launches the exact bound backend in restore verification mode and restores the
web child only after guarded acceptance and mandatory recovery. The supervisor
and boot preference remain in place. Guarded startup resolves configuration and
execution from the original journal, and terminal admission validates that same
binding. Existing canonical checkpoint and pre-acceptance rollback rules remain
authoritative; management recovery never rolls back database contents.

## Explicit operator recovery

`sudo streamhome recover --operation <uuid> --review` invokes the existing
canonical planner as the service UID. It binds the original execution, journal,
canonical plan identity and current OS stop intent. Review does not normalize or
change the operation. Declined and stale consent cannot launch the controller.

An approved action runs inside a cancellable root issuer scope. Its protected
900-second grant binds that exact operation, generation, action, evidence,
execution/control identities, boot, stop epoch and original parent binding.
Support contract version 7 explicitly declares parent recovery version 1; an
older bound reader cannot acquire compatibility by inference. Root starts only
the fixed operation unit. The UID controller uses the existing canonical recovery
engine, transfers ownership, preserves operator receipts and rechecks live
permission throughout controller and supervisor mutations.

Same-supervisor continuation preserves irreversible database acceptance. Rollback
is available only when the canonical planner proves the pre-acceptance checkpoint
boundary. Safe abort requires positively offline runtime evidence and no destructive
artifacts. A surviving supervisor with no application children still counts as
active, preventing an abort that leaves admission fenced.

The original supervisor fences a proven dead matching controller under the
canonical controller lock and preserves the same generation, capability, execution
and acceptance. It then stops only its owned application children and serves
maintenance. This never launches recovery or retries database activation. A
living owner/lease, replaced supervisor or ambiguous original handoff is not dead
controller proof.

## Explicit stopped-parent resumption

Support contract version 8 declares parent recovery versions 1 and 2 and OS
resumption plan versions 1 and 2. A stopped forward recovery needs the original
bound reader to explicitly support this protocol. Older readers remain refused.

A new live root scope emits a fresh review challenge. Noninteractive schema-3
consent must name that challenge, exact parent/generation/evidence and explicit
service resumption. Earlier schema-1/2 recovery consent cannot approve a start.
The root grant binds one plan, original parent binding, execution and requested
canonical recovery action. Boot preference is preserved.

The existing canonical recovery engine transfers the original parent ownership.
Before any OS start, an independent UID observer verifies the exact live
controller, operator action receipt, original journal/tuple, grant and process
quiescence. The fixed root bridge consumes the start permission before calling
systemd; ExecStartPre repeats the independent proof and binds that single OS
invocation. Lost replies and already consumed starts cannot automatically retry.

The replacement supervisor enrolls through that ticket without rewriting the
journal's original supervisor history. Its in-memory binding requires live scoped
permission and the exact current controller while the parent is unresolved. It
starts only the original guarded backend and a maintenance responder. A narrow
parent-only guarded-readiness query releases systemd's start wait while ordinary
application admission remains fenced. The controller then follows the existing
guarded database acceptance and mandatory recovery sequence. Successful completion
requires the actual owned web proxy to respond before authority is released.

Only exact accepted terminal evidence can settle the process's in-memory
enrollment after issuer exit. A new process cannot inherit that exception. A freshly approved takeover reads
its new generation grant rather than requiring a still-live previous issuer.
A dead
issuer, direct Stop or expired unresolved permission fences further effects;
continuation needs another fresh review. Bound environment reconstruction requires the separate repair permission below;
resumption does not authorize repair or a second database recovery algorithm.
Exact root control reconstruction remains a separate integration gate.


Independent read-only status queries can briefly spawn OS inspection helpers.
Stopped resumption may wait up to two seconds and repeat the entire quiescence
proof after they exit. This grants no exemption to live processes: persistent
foreign work, unknown identities and unreadable metadata still block effects.


## Exact environment repair under the original restore

After stopping the service, review `sudo streamhome repair --operation <uuid>
--review`, then explicitly approve that same operation's repair. Support contract
9 declares parent repair grant 3 and environment repair version 1. Older readers
without that declaration cannot borrow MANAGEMENT repair or resumption permission.

The independent service-UID reader works without the application's Python or npm
environment. Its read-only review binds canonical recovery-required authority,
controller absence, the immutable original execution, journal, verified recovery
cache and exact observed filesystem. A finite root scope issues a distinct repair
grant; it grants no database recovery, service start or parent completion.

The existing restore controller transfers its original lease and generation and
keeps recovery-required authority. Under that custody, repair preserves damaged
trees as contained evidence and reconstructs only the exact original Python, Node,
web and tool inputs at their recorded permanent paths. Acceptance, database,
configuration and active execution remain unchanged. A durable exact result
records finished artifact work while the original restore remains unresolved.

Write-ahead repair intents bind the root grant and original journal. They explain
an exact interrupted ownership transfer and permit only its journal generation
and timestamp to catch up before another separately approved transfer. Repeated
interrupted retries cannot accumulate unexplained journal lag. Expired old grants
can explain retained evidence, but only a new live grant can authorize more work.
Missing or contradictory evidence blocks repair rather than inferring ownership.

After repair, request a fresh `sudo streamhome recover --operation <uuid> --review`.
Any necessary stopped-runtime start still requires the separate schema-3
resumption challenge. Repair itself never resumes restore or reopens admission.
