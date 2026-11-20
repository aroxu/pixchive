# archiver — NFS TRIAL

Standalone deployment for Ubuntu 26 amd64, Docker 29 / Compose 5, operator
`aroxu` with Docker access and `sudo -n`. This directory contains deployment
artifacts only. The parent operator owns SSH, migration, image preparation,
runtime validation and the project AGENTS/PLAN/validation records. Runtime evidence
is recorded in `docs/validation.md` (2026-10-27 archiver and 2026-10-28 cache sections).

Current deployment target: `http://archiver:28000`, app image
`pixchive:archiver-media-jitter-20261120`, helper image
`pixchive-browser:archiver-nasfix-20261027`. The existing admin, Pixiv connection
and original 29-work archive were copied; the source installation is preserved.
`pixchive.service` and the hourly `pixchive-backup.timer` are enabled. A real
bookmark collection may continue independently of the deployment checks.

Media delay range (2026-11-20): the per-request delay now samples uniformly from
0.1–1.3 seconds, replacing the 0.1–0.4-second range below. The change only adjusts
the media sampler's upper bound. Metadata request pacing, three-download
concurrency, artwork budget, server cooldowns, cancellation, schema and UI remain
as before. The private release directory is
`/mnt/storage/pixchive/releases/media-jitter-20261120`. To revert only the new
range, restore `pixchive:archiver-media-jitter-20261106` and recreate web/worker;
both images use schema v11.

Media-request jitter (2026-11-06 follow-up): every managed media HTTP request
waits independently for 0.1–0.4 seconds before sending. This includes originals,
avatars, ugoira sources, redirects and retries. The session samples once per
request; if a peer extends the 429 cooldown while it sleeps, the request also
keeps its sampled delay after the new deadline. Waits remain cancellable and
long cooldowns abort before another request is sent. Up to three downloads can
still read bodies concurrently; images remain excluded from the artwork budget.
Only transport.py changed from the previous request-jitter release. Both app
services were replaced together, preserving 4,349 artworks, 34,885 assets,
administrator, key, Pixiv connection, settings, NAS/cache mounts and the hourly
full-media backup timer. The one-second metadata interval and 10-artwork limit
remain. No account job was active at cutover. The release is retained at
`/mnt/storage/pixchive/releases/media-jitter-20261106`; to undo only this media
delay, select the preceding v11 `pixchive:archiver-request-jitter-20261106` image
and recreate web/worker. No DB downgrade is involved.

Request-timing update (2026-11-06): ordinary Pixiv API/identity/list requests keep
the configured interval as their minimum and add 0–30% jitter, capped at one extra
second. A one-second setting therefore yields gaps of 1–1.3 seconds, excluding
network/processing time. Allowed same-origin identity redirects use the same gap.
Retryable 429/network failures add 0–1 second after the larger of Retry-After and
exponential backoff, within the existing 300-second automatic-wait boundary.
Long server cooldowns and final failure notifications retain their prior behavior.
This does not establish that Pixiv rate limits are avoided.

Only web.py and transport.py changed in this release. Web/worker were replaced
together with schema v11, UI, settings, per-minute artwork budget, three-page
media concurrency, NAS/cache mounts and hourly full-media backups retained.
The operator's one-second interval and 10-artworks-per-minute setting are intact.
No account job was running at cutover: the observed job had already succeeded.
All 4,349 artworks, 34,885 assets, administrator, key and archived metadata were
preserved. Helper/Redis were not recreated. The private release directory is
`/mnt/storage/pixchive/releases/request-jitter-20261106`.

The timing change can be rolled back to the preceding **v11** image
`pixchive:archiver-collections-20261030` by restoring that image setting and
recreating web/worker. No schema downgrade is involved in that rollback. The v10
downgrade warning below remains applicable only when going back to v10 binaries.

Saved-collections update (2026-10-30): the user requested the collections feature
on archiver too. Web/worker now include the tested v11 API and current UI, with a
single bookmark picker, undeletable Favorites default list, collection-creation
FAB, desktop dialogs and mobile sheets. Migration 011 adds a false-by-default
favorite column/index and two empty list tables. An isolated copy of the actual
v10 DB passed migration twice with all 29 preexisting table contents preserved.
The real upgrade retained 4,189 artworks, all 33,554 assets, administrator, key,
Pixiv connection and settings; only the worker heartbeat setting changed.
The operator's configured 10-artworks-per-minute limit, sequential artworks with
up to three image pages downloaded together, cache/NAS paths and hourly full-media
backup policy remain. Helper and Redis were not recreated. The in-flight v10
hourly backup retains its already-fixed snapshot; later invocations select the
new image from `.env`. Production lists and favorites were left empty; creation
and multi-list saves were exercised only in isolated QA.

**Schema rollback:** v10 binaries reject a v11 database. Do not merely change the
image tag back. The private release directory
`/mnt/storage/pixchive/releases/collections-20261030` retains `previous.env`, the
pre-upgrade `preupdate.sqlite3` and its state record. Returning to v10 requires
stopping all DB writers/backup execution and restoring the chosen v10 recovery
point into a new empty target with the v10 tooling and matching original keys.
Keep later user writes separately; restoring the older point cannot retain them.
The normal recovery procedure below still applies. The current v11 release can
restore old v10 backups by migrating them forward; that is not a downgrade.

Request-limit update (2026-10-29): web/worker use the worker-tuning image plus a
per-minute cap on Pixiv artwork lookups (default 5, 1–60 in the schedule card of
Settings). Only artwork information lookups count; bookmark list pages and image
downloads do not. The one-minute window is stored in the DB and shared by every
account job, so it holds across jobs and worker restarts. A job at the cap shows
the waiting phase and the next allowed time and still honours pause/cancel within
a second. That release kept schema v10 and the rest of the UI unchanged. It is
now superseded by the v11 collections release above, including the schema rollback
requirements. The request-limit implementation itself does not add a migration.

User rollback (2026-10-28, recorded 2026-10-29): rate limits were reported, so
web/worker were restored from artwork-workers r3 to the preceding worker-tuning
image. The DownloadTask queue, failed jobs and retry timestamps are preserved.
The scanner checks one bookmark page, then downloads its artworks one at a time
before fetching the next page. Up to three pages of the same artwork can still
download together. Eight-artwork concurrency and the independent scanner are
disabled. Cache/NAS publication, independent lease heartbeats and hourly full
backups are retained. Existing rate limits require their own cooldown; this
rollback does not establish that Pixiv has lifted them. See `docs/validation.md`
for database, asset and account preservation checks. The earlier r3 implementation
and throughput figures are historical and do not describe the current deployment.

The parallel-pages update replaces web/worker together and retains the jobs-first
query fix. Up to three original image pages from one artwork download together;
cache/NAS publication remains serial. Completed responses are bounded to one
batch, and a smaller window is used when the three-page space budget will not fit.
429 cooldown gates new requests, redirects and retries across the shared media
session. This release preserves schema v10 and the existing UI.

The worker-tuning follow-up renews job ownership from independent, connection-owning
heartbeat threads with 120-second leases. Threads stop and join before shutdown
releases DB/storage access. Progress accounting is combined per bounded group,
avatar publication workspaces are reused, and unchanged ready-file/status checks
avoid redundant DB writes. Interrupted first-page previews are recovered through
bounded, deduplicated preview jobs on resume. Hashing, fsync, archive paths and
hourly full-media backups remain enabled.

The target NAS overrides creation permissions. Browser ciphertext and recovery
files explicitly apply mode 600 before writing. Temporary workspaces close their
ownership descriptor after publishing the cleaning name and before unlinking;
otherwise NFS retains an open file as `.nfs*` and cleanup fails with ENOTEMPTY.
These fixes do not establish SQLite WAL support on NFS.

User update (2026-10-28): keep **hourly full-media backups**. Bind host-local XFS
`/cache/pixchive` to `/cache` in web/worker, with `PIXCHIVE_CACHE_DIR=/cache`.
Working originals, avatar staging and ugoira frames/video use local scratch.
Publication copies into a private NAS workspace, verifies actual copied size and
SHA-256, then atomically renames within NAS. DB, finished originals/derivatives,
keys, encrypted login state and all backups remain at the existing NAS paths.
The cache is disposable working data and is never the archive or backup authority.
Both filesystems must have enough free space; managed abandoned workspaces on
both cache and NAS are recovered using the same ownership locks/markers.
The backup-only override disables the web HTTP healthcheck: its one-off CLI
container does not run an HTTP server. Ordinary web readiness is unchanged.

Prepare `/cache/pixchive` with mode 700 on local storage before installing the new
Compose. `start.sh` rejects a missing, linked or non-local cache path. Units require
both mount paths. Earlier collection-only releases kept schema v10; the current
release includes saved collections and schema v11 as requested on 2026-10-30.

**Explicit user exception:** all persistent application state, including SQLite,
WAL/SHM and process locks, stays on NAS at `/mnt/storage/pixchive`. SQLite WAL on
NFS is unsupported. This is an **NFS TRIAL**, never a stable/supported deployment;
mount checks, successful backups and `/health` cannot establish NFS locking,
durability or crash safety. There is no local DB, block-image or PostgreSQL
fallback and no product change. Never run another installation against this DB.

## Paths and preparation

`PIXCHIVE_ROOT` can change the canonical absolute NAS root. Every business bind
uses long syntax with `create_host_path: false`; missing storage is not created.
The additional `/cache/pixchive` bind is host-local working space, not NAS persistence.

| NAS path under root | Container / purpose |
| --- | --- |
| `data/` | One `/app/data` bind in web and worker: DB/WAL/SHM, media, avatars, previews, NAS publication/older workspaces and locks |
| `secrets/` | Read-only directory in web/worker; helper gets only read-only `credential.key` |
| `browser-state/` | Helper `/state`, including the original `auth-state.enc` |
| `redis/` | Redis `/data`; AOF enabled, `appendfsync everysec`, RDB disabled |
| `backups/` | Web `/backup`; CLI DB snapshots, incremental media mirror and private recovery bundles |
| `security/seccomp_profile.json` | Unchanged official Playwright seccomp profile, referenced from the NAS root |
| `data/.pixchive-storage-id` | Required operator-created regular marker; startup never creates it |

The parent prepares these directories with mode **700**, migrated regular files
with **600**, and the marker only after confirming the intended mount and copied
archive. Keep the existing 29 artworks, originals, admin DB, original encryption
key, encrypted Pixiv credential and helper ciphertext. Do not initialize a blank
database or regenerate a key. Migration and permission changes are outside these
scripts. NFS maps all UIDs to 3000; chmod 600/700 and root reads of 600 files were
confirmed by the parent. Do not try recursive chown. Redis runs as `1000:1000`
and bypasses the root entrypoint's chown. The existing helper launcher reads the
key initially as root and permanently drops to `1000:1000`.

Install these artifacts at `/mnt/storage/pixchive/deploy` (or adjust unit paths).
The parent installs the bundled, unchanged `security/seccomp_profile.json` at
`$PIXCHIVE_ROOT/security/seccomp_profile.json`. It is a byte-for-byte copy of
`browser_helper/vendor/seccomp_profile.json`; provenance and the upstream license
are included alongside the copy in `security/`.

Copy `.env.example` to `.env` and select the parent's prepared images. Example
fixed trial tags are placeholders for local image preparation, not evidence of
published images. Web/worker always use the identical required `PIXCHIVE_IMAGE`;
helper requires `PIXCHIVE_BROWSER_IMAGE`. Prefer immutable digests when available;
Redis's example tag can also be replaced with a digest. No build or pull occurs
in these scripts. `.env` is trusted, shell-compatible **public metadata only**;
it is sourced by the wrapper and takes precedence over exported variables.
`PIXCHIVE_ENV_FILE` can select another such file. Do not put credentials in it.

## Commands

Run from the installed directory after the parent has migrated and checked data:

```sh
sh ./start.sh preflight
sh ./start.sh start
sh ./start.sh status
curl --fail http://127.0.0.1:28000/health
sh ./start.sh backup
sh ./start.sh stop
```

Preflight only inspects mount/type, canonical paths, file existence/permissions,
restore markers, official seccomp equality and Compose syntax. It requires
`PIXCHIVE_NFS_TRIAL=1` and NFS at every persistence path. It does not create paths,
read credentials, open the DB, contact Pixiv or prove readiness. Web/worker also
require the marker, acknowledgement and existing DB at each container start,
using `sh`, `umask 077`, `exec` and a 60-second graceful stop. Stop skips preflight
so a storage outage does not prevent requesting shutdown; it never deletes data.

Only web publishes `0.0.0.0:28000` to container 8000. Ports 8000/18000 on the host
are unused by this stack. Default origin is `http://archiver:28000`; list other
exact origins in `PIXCHIVE_ALLOWED_ORIGINS`. Helper/Redis have no published ports.
Helper retains sandboxing, 512 MiB shared memory and UID/GID 1000 tmpfs at
`/run/browser` and `/tmp`. Web `/health` only reports HTTP liveness; Redis PING
only reports Redis liveness. Worker heartbeat, login state and actual archive
operations still need parent runtime checks. No extra helper health token or
credential is exposed. Resource limits are left unset; the parent can tune them
after observing this 4-CPU host with 7.2 GB total / 2.8 GB available memory.

Each service uses standard Docker stdout/stderr with per-container `json-file`
rotation (10 MiB, three files). Docker-managed container layers/logs remain in
the daemon's normal storage outside business data; no global Docker change is
made. Container restart policies cannot rerun host preflight: after a NAS outage
or Docker restart, stop the stack, recheck the actual mount and use this wrapper.
The startup marker is a presence guard, not a mount-health or identity proof.

## Backup and recovery

`start.sh backup` uses a disposable web-image container with the backup-only
override: helper state is mounted read-only only for that invocation. It runs
the official `python -m app.cli backup --output /backup --media`, preserving the
online SQLite snapshot and incremental media mirror. `backup.py` adds a mode-700
`recovery/` directory inside the returned snapshot, containing byte-exact original
`credential.key`, optional `setup-token`, optional `auth-state.enc`, the storage
marker as `storage-id`, and a presence manifest. Files are mode 600. Secret bytes
are never printed. The original key is checked for changes during the backup.
The helper's atomically replaced ciphertext is captured independently; it is not
transactionally synchronized with the SQLite snapshot. If it is absent (for
example after explicit disconnect), the manifest records its absence.

Only CLI exit 0 plus `.recovery-complete` confirms a complete deployment recovery
point. Exit 1 preserves a partial media snapshot and recovery files but remains
a failed backup. Exceptions or termination may leave incomplete hidden staging
directories; preserve and inspect them after all backup processes have stopped.
No retention/deletion runs. Keep each chosen snapshot, its `recovery/` and shared
`media-mirror/` together. This is a same-NAS recovery copy, not independent NAS
failure protection; the parent should copy the complete set elsewhere privately.

For recovery, stop web/worker/helper and backup execution first. Verify the chosen
snapshot using the official `verify-backup SNAPSHOT --hash`, then use official
`restore --from SNAPSHOT --verify-hash` into a **new empty** data directory.
The wrapper intentionally rejects an empty target; the parent performs recovery
separately using the image CLI and existing documented procedure. Restore the
matching original key to `secrets/credential.key`, the optional setup token, and
the matching ciphertext to `browser-state/auth-state.enc`; reinstall the marker
from `recovery/storage-id` only after checking the new NAS target. Retain private
modes. Never copy just a running WAL DB, remove WAL/SHM to force startup, delete
an incomplete-restore marker, or merge a failed restore into a live target.
Redis AOF is persisted but is not the archive backup authority; DB/outbox recovery
uses the application's existing behavior. On disconnect/lock/I/O errors, stop
and preserve evidence/originals, restore NAS availability and rerun preflight.
Do not claim crash recovery validated merely because a container restarts.

## Optional systemd

Units default to `/mnt/storage/pixchive/deploy`, `User=aroxu` and
`RequiresMountsFor=/mnt/storage/pixchive`, with Docker/network ordering. If changing
the root, update `RequiresMountsFor` in **both** services; systemd does not expand
the `.env` root there. Install only after migration and parent runtime approval:

```sh
sudo -n install -m 644 systemd/pixchive.service systemd/pixchive-backup.service systemd/pixchive-backup.timer /etc/systemd/system/
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now pixchive.service pixchive-backup.timer
sudo -n systemctl start pixchive-backup.service
sudo -n systemctl status pixchive.service pixchive-backup.timer
```

The timer runs hourly (up to 30 seconds jitter), catches up after downtime, and
does not overlap the same systemd backup unit. Observe backup exit status in the
journal; failed backup services do not establish a recovery point. For a planned
stop, stop the timer, allow any running backup to finish, then stop
`pixchive.service`. All stop actions preserve bind data; none use `down -v`.
