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

Explicit parent operator recovery, stopped/replacement-supervisor resumption and
parent-bound environment/control reconstruction remain integration gates. The
initial response/ownership protocol does not authorize those actions.
