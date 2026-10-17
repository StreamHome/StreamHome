# Storage Management

The Storage workspace keeps local storage active and optionally offloads eligible
video and separate audio files to Google Drive. Connecting Drive and enabling
offloading are separate actions. Subtitles, artwork, catalog metadata and the
database stay local.

Open **Admin Center → Storage & HEVC**. The existing admin navigation remains at
the top. The workspace has three views:

| View | Purpose |
| --- | --- |
| Overview | Local and Drive capacity, total budgets, connection, local retention and cleanup |
| Policy | Automatic offloading, placement rules, next-episode preparation and HEVC settings |
| Activity | Accepted operations, progress, released bytes and blocked work |

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

Budgets start unset. Enter the desired values and units, select **Review changes**,
then save the exact reviewed draft. The server checks the policy revision and
Drive target again. Saving a policy does not mean that files have moved or space
has been released. Use **Activity** to inspect the actual outcome.

**Refresh usage** requests a measurement. Displayed stale values remain labelled
as last known measurements. If a mutation response is lost, use **Check status**;
it checks the original request rather than repeating the change.

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
