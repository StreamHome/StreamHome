# Storage Management

The Storage workspace keeps local storage active and optionally offloads eligible
video and separate audio files to Google Drive. Connecting Drive and enabling
offloading are separate actions. Subtitles, artwork, catalog metadata and the
database stay local.

Open **Admin Center → Storage & HEVC**. The existing admin navigation remains at
the top. The workspace has three views:

| View | Purpose |
| --- | --- |
| Overview | Local and Drive capacity, budget summaries, connection, local retention and cleanup |
| Policy | Total budgets, automatic offloading, placement rules, next-episode preparation and HEVC settings |
| Activity | Accepted operations, progress, released bytes and blocked work |

Capacity and file sizes use binary units: 1 KiB = 1024 bytes, 1 MiB = 1024 KiB,
1 GiB = 1024 MiB and 1 TiB = 1024 GiB. Budget inputs use GiB or TiB. Previously
saved budgets retain their exact byte values when displayed in these units.

## Set total budgets

The local budget covers application-managed media, playback caches, temporary
processing files, database/WAL, managed backups and recovery evidence. It is a
total allocation, not a maximum individual file size. StreamHome also preserves a
physical free-space reserve on every filesystem it writes to. Other applications'
files affect physical free space but are not counted as StreamHome-managed data.

The budget controls admission to new storage work. Essential database/recovery
growth, external additions or an existing overage can exceed it; protected data
is preserved and new work is limited. This does not install a filesystem quota.

The cloud budget applies to the selected Drive folder, including existing files
and temporary upload copies. The Google account's available quota is a separate
limit. A configured budget does not reserve space at Google or increase account
capacity. Unknown or stale measurements cannot authorize an upload.

Budgets start unset. Open **Policy**, enter the desired values and units, select **Review changes**,
then save the exact reviewed draft. The server checks the policy revision and
Drive target again. Saving a policy does not mean that files have moved or space
has been released. Use **Activity** to inspect the actual outcome.

## Automatic measurements and status

Storage measurements run in the background even when automatic offloading is
paused. Local usage is measured after startup and approximately once a minute;
the configured Drive folder and account quota are checked approximately every
five minutes. Completed storage work requests a coalesced update. Slow or failed
checks do not create overlapping scans or a new job for every open browser tab.

The page displays the latest available values immediately and automatically reads
new status while visible. It checks more frequently during active work, pauses
browser polling while hidden, and checks again when you return. Ordinary usage
measurement counts file sizes; content verification remains a separate requirement
before accepting transfers or releasing local originals.

Each storage target reports its own last successful measurement and current check
state. A failed check preserves the previous successful values with their age and
an explanation. A target that has never been measured is shown as unknown, rather
than empty or full. Measurements do not lock unrelated policy fields. A budget
summary without a configured limit is labelled unrestricted; a usage distribution
is not presented as a percentage of a nonexistent limit.

Accepted background work is tracked automatically. If a mutation response is lost,
the page checks the original request identity without repeating the change. An
unresolved outcome remains visible and conflicting actions stay protected. Expired
admin authorization pauses protected reads until you confirm your identity;
background polling does not extend authorization. Policy drafts remain intact
while measurements update. Saving policy, granting Drive access, verifying a folder
with a temporary file, and confirming cleanup remain explicit actions.

Dashboard measurements do not replace the stricter fresh capacity, generation and
protection checks required before storage work is admitted.

## Automatic placement

Automatic offloading starts at 90% of the local budget and works toward 80%.
Capacity denied to a new writer can also request cleanup. Written data and the
remaining capacity reserved for ongoing work both count toward admission.

The maintenance order is:

1. Remove eligible idle, rebuildable playback caches.
2. Release eligible local copies already backed by a verified cloud generation.
3. Upload eligible cold video or separate audio, verify the exact content, record
   durable acceptance, then remove the matching local copy.

Playback, explicit local retention, ongoing processing and unresolved recovery
take precedence. A paused playback run remains protected. Provider failures,
changed sources and insufficient capacity block work; they do not authorize
removing an original. Pausing offloading leaves existing cloud media readable.

## Playback and Keep local

Cloud-backed direct playback uses protected byte-range requests. Adaptive playback
may need a complete source spool plus baseline video and audio outputs. Its peak
space must fit the local budget and physical reserve. A large remote file is not
guaranteed to play under a smaller local budget.

Missing adaptive caches for the same accepted source generation can be rebuilt by
owned, deduplicated background preparation. Source relocation preserves accepted
identity; changed content requires verification. Explicit Pause remains in control
while status and preparation progress refresh.

**Keep local** requests installation-wide retention on this server. It is not a
download to the browser or phone. Offline readiness is reported only after the
source and default audio are local and the required baseline is verified. Removing
a keep request does not immediately delete files. Optional next-episode preparation
is limited to one episode ahead and remains subject to capacity and protections.

## Cleanup and recovery

**Review cleanup** presents a bounded plan. Confirming it rechecks source
generations, current protections, policy and Drive target before releasing space.
Estimates and released bytes are separate values. Existing unrelated Drive files
are counted but are not cleanup targets.

Accepted cloud placements have permanent recovery metadata under
`server/media/.storage-placements`. Preserve this directory with the rest of the
media metadata. It survives restoring an older database and must not be removed
as a temporary cache. Pending transfers and reservations must settle before a
database restore. Unknown outcomes retain evidence and conservative capacity until
positive observations resolve them; accepted final cloud media is not deleted as
compensation for a failed temporary cleanup.

Existing installations keep their legacy LOCAL/CLOUD behavior until a hybrid
policy is deliberately saved. The old storage-mode settings writer then rejects
changes so it cannot bypass the reviewed policy. Connecting Drive alone does not
enable offloading or import an existing library.

See [Google Drive connection](google-drive.md), [playback](playback.md) and
[backup and recovery](backup-and-recovery.md).
