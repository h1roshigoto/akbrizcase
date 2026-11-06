# MVP-042 — encrypted backup and closed restore

**PARTIAL / PRODUCTION RELEASE BLOCKED.** The implementation and dedicated CI
job are synthetic rehearsal tools. No RF offsite provider, credentials, legal
retention approval, independent production journal or target restore measurement
was supplied. ADR-038-A remains PROPOSED. No merge, deployment or MVP-043.

Baseline: `d03ad1211a2c676686c40037ceb4085d174fd054`.
Schema: `0011_mvp039_http_security`; no migration or public API change.
Exact tested HEAD and aggregate measurements are emitted as `BACKUP_EVIDENCE`
by the dedicated Actions job; a test's existence is not evidence of execution.

## Snapshot and trust boundary

`app.backup.snapshot.capture` uses one PostgreSQL 16 REPEATABLE READ transaction.
The exported snapshot is passed to `pg_dump --snapshot`; the document/image
inventory is selected in the same transaction, never after an independent dump.
All app-schema tables, data, ACLs, triggers, constraints, functions, indexes,
sequences and migration state are dumped; server roles/passwords are not dumped.

Order: shared filesystem deletion lease → nonblocking staff advisory lock 4004 →
`SHARE NOWAIT` table locks on catalog_items, documents, document_versions →
existing active-admin authentication with migrator credentials → exported snapshot
and referenced files/dump. There are no content/cleanup/row locks in the backup.
Existing lifecycle writers use staff/content/row locks unchanged. Any table lock
conflict aborts immediately, so backup cannot form a wait cycle with those writers.
Readers remain available; competing file-reference/visibility writes wait only
for the bounded copy phase. CLI has a 600s process deadline, pg_dump 120s timeout,
SQL 120s statement and 130s idle-transaction limits. Encryption and storage I/O
occur after releasing all database/filesystem locks. Failures/cancellation close
the UoW and temporary dump directory; acceptance is a separate final step.

Verified objects are immutable, created exclusively and never overwritten by
existing application code. There is currently no physical-file cleanup worker;
MVP-038 cleanup deletes database personal data, not stored documents/images.
Any future physical file remover MUST hold `deletion_lease(root, removal=True)`
through reference checks and removal. Backup holds the shared lease until every
reference has been copied and rechecked. Direct administrator edits are outside
the supported lifecycle, but strict reopen/checksum/size checks catch interference.

Every bound document version is saved, including internal, retired, revoked and
bound draft versions; restoration does not publish them. Every catalog image_key
still referenced by a row is saved, preserving archived-row metadata as well.
Unbound verified or quarantine uploads are excluded. Document/image receipt
sidecars are included and checked against metadata. Source components are opened
with directory descriptors/O_NOFOLLOW, owner/mode/nlink/regular-file/size checks.
File keys are closed-format server identifiers, never caller paths.

## Format, encryption, keys and storage

Age v1 through pinned `pyrage==1.3.0` (maintained Rust age implementation) provides
standard authenticated encryption. The entire tar, including manifest and dump,
is encrypted. There is no custom cipher/KDF/nonce construction. Limits are explicit:
256 MiB archive and 10,000 file members; larger installations fail closed and need
a reviewed streaming extension. This is not a production scale claim.

Manifest v1 contains schema revision, full app Git HEAD, exact application image
config digest, PostgreSQL major, conservative transaction snapshot time, sequence
floor and optional source/epoch binding, dump SHA256/size and each file's technical path/SHA256/size/mode. No customer
text, names, audit details, tokens or keys occur in it. It remains private inside
the encrypted archive; do not print or upload it. Files are mode 0400, directories
0700. The restored database carries original ACL/lifecycle metadata. Extraction
never uses tar.extract: only exact allowlisted regular members with no duplicate
names or traversal/links are accepted. Each checksum and size is checked before
restoring; sources are rechecked against restored DB references afterwards.

Identity files are owner-only 0600 under a protected directory, separate from
archives and journal. CLI receives their path, never key bytes/passwords in argv.
Admin credentials use the existing hidden terminal input. PostgreSQL passwords
enter pg_dump/restore through a temporary 0600 pgpass, never argv or output.
The CLI captures all child output and returns only closed codes. Temporary dump,
pgpass and extraction staging directories are cleaned on errors; failed DBs stay
isolated and their services are stopped by the controller. Power loss leaves
unaccepted encrypted objects or a quarantined volume, never a ready receipt.

`BackupStorage` is the narrow trusted adapter seam: immutable publish → durable
read-back → age authentication and exact manifest/ciphertext comparison → accept.
`SyntheticStorage` models this using fsync/atomic writes in a private directory.
A failed upload/read/check/decryption/accept has no accepted-copy record; encrypted
orphans are unusable until an explicit new verified operation. It is **not offsite**.
A production provider must implement authenticated durability and conditional
publication using independent RF infrastructure and separate credentials.

Key rotation: provision a new age identity outside Git, rehearse recovery with it,
then select its public recipient for subsequent copies. Retain protected old keys
until every permitted backup encrypted for them has expired under approved policy.
Do not overwrite the only recoverable identity. Actual custody/escrow and rotation
approval are operator responsibilities and remain BLOCKED without that setup.

## Closed restore and deletion completeness

`compose.backup.yml` is standalone. The controller claims a random namespace,
rejects existing/external volumes and checks actual PG mounts. Source and target
are separate fresh PG16 instances with six independently generated passwords. A
second empty target database and independent files volume exercise incomplete
journal failure without reusing the successful restore destination.
The target bootstrap creates roles and pg_trgm, no app schema or application rows.
A new files volume must be empty. There are no API/worker/SMTP services, no ports,
no external network, production mounts or production credentials in this profile.

Restore checks environment, fresh DB/files, age authentication, exact supported
schema/HEAD/digest, then runs pg_restore in one transaction. It restores files
privately, checks every FK against actual data, validates the historical MVP-038 NOT VALID
CHECK against restored rows, checks erasure
postconditions and references/checksums, and advances the sequence under a sequence
lock beyond restored rows, its existing
state, the backup floor and a verified external source high-water mark. Validation
does not allocate a number (gaps are permitted).
Only after full journal replay and repeated checks does it return
`synthetic-ready-for-manual-review`. It never starts ingress or opens user access.
A failure after database import leaves that database quarantined; do not reuse it
as an empty target. Start another fresh instance for a subsequent restore.

`CompleteSyntheticJournal` preserves MVP-038 `JournalSink`, durable acknowledgement,
VerifiedWitness, reconciliation and postconditions. Every export is durably indexed
before acknowledgement. Full-history discovery requires all indexed objects,
matching UUID/canonical bytes and an independently supplied source-time checkpoint
(epoch, count, inventory digest, fenced_at). The checkpoint is obtained AFTER the
last relevant source deletion, outside the backup; restore cannot manufacture its
own anchor from surviving files. A lock fences new exports through final checks. Replay accepts only a live
process-local VerifiedInventory from that checked fence, bound to its sink; a
caller-supplied list or expired inventory cannot authorize replay.
Local journal entries are cross-checked but never substitute for missing external
witnesses. Post-snapshot commands use the migrator restore seam and exact original
UUID/bytes; manual full delete reauthenticates active admin. Chunked deletion is
bounded; UNCONFIRMED/CONFLICT/REVIEW_REQUIRED never passes. Policy, legacy, hold
uncertainty and unbound service records retain existing fail-closed behavior.

The source must be stopped/fenced, and every mutation writer must use the indexed
sink, before obtaining a checkpoint. This local synthetic model cannot prove
provider independence, nonrollbackable source fencing or authenticated freshness
following a host loss. Production discovery/checkpoint authentication, complete
legacy import and durable terminal acknowledgements remain **BLOCKED**. There is
no production enabling flag. Witnesses and unresolved PREPARED never expire or
prune automatically; elapsed 14 days cannot authorize deletion.

## Operators, scheduling and retention

Run the disposable rehearsal from the repository (no production data):

```sh
make backup-rehearsal
# Optional aggregate-only report, with a caller-selected safe output path:
make backup-rehearsal BACKUP_REHEARSAL_ARGS='--report /tmp/mvp042-measurements.json'
```

The controller builds exact-source tools, runs actual encrypted dump → fresh
restore → replay → ACL/business checks, then stops all containers while retaining
volumes for protected inspection. No secrets/dumps/archives/manifests/raw logs are
CI artifacts. Local failure diagnostics stay under a private /tmp directory.
`--reuse-image sha256:...` is a development-only immutable tool-image selector;
release evidence must use a clean checkout and a newly verified exact-source build.

Protected one-shot CLI operations (only inside a claimed synthetic operator
container, with read-only secret mounts and the existing DB environment):

```sh
python -m app.backup backup --store /protected/synthetic-store \
  --identity-file /run/secrets/backup-age-identity --files /private/files \
  --head FULL_REVIEWED_HEAD --image-digest sha256:REVIEWED_IMAGE_DIGEST \
  --sequence-authority-dir /protected/sequence \
  --sequence-key-file /run/secrets/sequence-authority-key \
  --diagnostics-receipt /protected/status/backup.json
python -m app.backup restore --store /protected/synthetic-store \
  --identity-file /run/secrets/backup-age-identity --files /private/new-files \
  --head FULL_REVIEWED_HEAD --image-digest sha256:REVIEWED_IMAGE_DIGEST \
  --backup-id TECHNICAL_UUID --journal /protected/independent-journal \
  --checkpoint /protected/source-fence-checkpoint.json \
  --sequence-authority-dir /protected/sequence \
  --sequence-key-file /run/secrets/sequence-authority-key
```

No uncontrolled daemon/timer is installed. These explicit bounded operations are
an operator mechanism, not proof of a daily schedule. Before production the owner
must configure a supervised <=24h timer, failure/missed-run watchdog and approved
remote adapter, then measure a restore there. Retention is policy-bound: proposed
14 days is not silently treated as legal approval. `prune` refuses to delete any
copy until policy, last-usable-copy safety, dependent journal terminal evidence and
all backup horizons are approved. The current absence of pruning retains the last
usable copy and every unresolved journal; capacity must be supervised meanwhile.

MVP-040 receipt v1 stays compatible. V2 adds actual snapshot time, operation result
and synthetic/offsite scope. A successful local rehearsal writes status=failure
for mandatory offsite verification and `BACKUP_OFFSITE_BLOCKED`, never production
success. Snapshot age and last actual verification age are separate. A crash or
missed schedule cannot refresh a success record; stale observations alert after
24 hours. No receipt replaces provider evidence or target RPO/RTO acceptance.

## Acceptance matrix and measurements

| DoD | Synthetic evidence | Production |
|---|---|---|
| One snapshot for dump/references; bounded locks | PASS: real PG16 snapshot/concurrency drill | Infrastructure acceptance BLOCKED |
| Files/sidecars/SHA256/private ACL and original visibility | PASS: clean restore/public and staff HTTP checks | BLOCKED |
| Standard authenticated encryption; wrong key/tamper/truncation | PASS: real age unit faults | Key custody BLOCKED |
| Durable remote verification | PASS: atomic synthetic adapter faults | Independent RF storage BLOCKED |
| Daily backup, <=24h watchdog, 14d retention dependencies | One-shot mechanism; conservative no-prune | Schedule/policy/provider BLOCKED |
| New PG16 and new files, FK/sequence/schema checks | PASS: actual pg_dump/pg_restore | Target drill BLOCKED |
| Full graph/PII/chat replay and idempotency | PASS: six post-backup v1/v2 commands | Independent journal/ADR/policy BLOCKED |
| Full-history completeness, missing/conflicting/stale checkpoint | PASS: live fenced inventory and negative cases | Authenticated source fencing/discovery BLOCKED |
| No SMTP/worker/ingress during restore | PASS: standalone profile and disabled SMTP assertions | Operator network review BLOCKED |
| Required regressions plus release rehearsal | PASS: all six jobs on verification commit below | No deploy or production claim |
| RPO <=24h, RTO <=4h | PASS rehearsal only: timings below | **BLOCKED** |

Measurements must be copied from a successful `BACKUP_EVIDENCE` record on the PR's
exact HEAD: backup/restore wall seconds, snapshot-to-control-state interval,
encrypted bytes, files checked, FK count/sequence checks, deletion replay count and
completeness scope. CI runner timings are rehearsal evidence only; they do not
verify RF storage, sustained schedule, production data scale or target RPO/RTO.

Initial local Docker VFS attempts hit ENOSPC before the drill; no PASS or measured
restore time is claimed for those attempts. Final local/CI outcomes are recorded
in the PR after running the checks, with no borrowed historical PASS.

Development native probe (before the final CI acceptance commit): two fresh PG16.13
clusters and private file roots; real ClamAV 1.4.6 and the existing sandbox, with
executables relocated to /tmp because Docker VFS exhausted the local disk.
One full test passed: backup 0.415s, restore 1.011s, snapshot/control interval
3.095s, encrypted size 143743 bytes, 10 files, 30 FKs, sequence PASS, six replayed
operations, v1/v2 and two real commit fault boundaries, incomplete restore BLOCKED.
This development probe used an uncommitted working tree and a synthetic image
selector; it is debugging evidence, not exact-image or exact-HEAD acceptance.
The dedicated clean-checkout CI job provides those measurements separately.

## Historical clean-checkout CI evidence (before P1-01)

Implementation verification commit: `9e14e795d7d1ce596ac8ded219bd6975cd35e1f7`.
[Full six-job run 42159676409](https://github.com/h1roshigoto/akbrizcase/actions/runs/42159676409)
completed successfully before the independent P1-01 review. Its snapshot-only
sequence assertion did NOT establish non-reuse of numbers issued after backup.
These historical results do not validate the correction below. The PR records the
new exact HEAD, new full six-job run and new actual measurements.

| Measurement on disposable CI runner | Observed |
|---|---:|
| Age-encrypted backup wall time | 0.418 s |
| Clean PG16/files restore, replay and gates | 1.203 s |
| Snapshot to final control-state interval | 6.320 s |
| Encrypted archive | 143655 bytes |
| Verified file/sidecar members | 10 |
| Explicitly checked foreign keys | 30 |
| Replayed post-backup operations | 6 |
| Journal wire formats / real commit fault boundaries | 2 / 2 |
| Incomplete-journal restore | BLOCKED; extracted files removed |
| CHECK validation / sequence / original ACL / SMTP disabled | PASS |
| Independent RF offsite verification / target RPO-RTO | **BLOCKED** |

Executed commands: `make backup-rehearsal`, `make backend-test backend-lint
backend-types`, frontend tests/typecheck/build, `make integration-ci`, Compose
build/smoke/HTTPS/browser suites and `make release-rehearsal`. Backend: 1009 PASS,
lint/format PASS, strict types 215 files PASS; frontend: 218 PASS and typecheck/build
PASS. The required PostgreSQL, Compose/browser and release jobs all passed.

The 6.320s interval is a measured synthetic recovery-point observation, not a
production daily scheduling guarantee. The 1.203s restore does not include building
tools or provisioning the disposable runner and cannot establish target RTO.
Production RPO <=24h and RTO <=4h require the owner's actual RF storage, key custody,
authenticated complete independent journal, supervised schedule and timed full
infrastructure recovery. All those gates remain BLOCKED; there is no public access.

## P1-01 — post-backup number high-water gate

A backup's sequence floor alone cannot prove non-reuse after disaster: the live
source can issue N+1 after a backup at N. Restore must not treat that older floor
or max(restored orders.number) as the all-time maximum, even after deletion replay.
`invariants` and `restore` now require a verified external high-water capability;
without it they fail closed and never return SEQUENCE PASS. No public write
admission is provided: the result explicitly keeps `new_order_admission=false`.

The narrow synthetic model is `SyntheticSequenceAuthority`, initialized BEFORE
backup in a private directory outside the DB and encrypted backup. Its random
source ID/epoch and a fingerprint of the live PG16 process/database are bound into
the authenticated backup manifest. Its state and checkpoints use standard
HMAC-SHA256 with a separate owner-only 32-byte key, never the application HMAC or
age identity. They carry only technical identifiers, the dump digest, counter,
fence nonce and issue time. An operator number, Boolean "fenced", bare JSON file,
unsigned number or checkpoint reconstructed after restore is not evidence.

Order of operations:

1. Initialize the authority only in an empty private directory on the marked
   synthetic source, before backup. The key must already exist as mode 0600,
   outside the archive. Add `--initialize-sequence-authority` only to the FIRST
   backup CLI call. Later backups load and authenticate the same authority state.
2. After the backup and all required source deletion operations, the active
   admin/migrator obtains the source fence: staff advisory → NOWAIT ACCESS EXCLUSIVE
   orders table lock → `ALTER SEQUENCE ... NO CYCLE`. The sequence is required to
   have increment/cache 1, positive bigint bounds and NO CYCLE already; this DDL
   changes no numbering policy and takes ShareRowExclusiveLock. It fences direct
   nextval/setval too, including allocations from other SQL connections. Acquisition
   is bounded; there is no reset, restart, downgrade, stamp or migration.
3. Only under those real PG locks, read the last potentially allocated number,
   compare it with the authenticated durable authority state and backup floor,
   fsync a monotonic authority update and signed checkpoint. A regression blocks.
   The checkpoint binds source ID/epoch/process, exact dump digest, fresh issue
   time and a new fence nonce. The authority never accepts a caller-supplied number.
4. Keep that source transaction/fence live through target restore, deletion replay
   and validation. A process-local `VerifiedHighWater` checks the MAC, protected
   durable files, source/epoch/dump binding, freshness, source backend/transaction,
   actual granted sequence lock and current source counter before admission checks.
   Signature alone cannot prove completeness. A missing/corrupt/stale checkpoint,
   wrong source/epoch, lost fence, unknown source history or lower maximum blocks.
5. In the closed target, serialize validation with its own sequence lock. Advance
   with setval only when needed beyond the greatest of restored row numbers,
   backup floor, external high-water and existing sequence state. Never lower the
   sequence, consume a probe number or infer historical maximum from surviving
   orders. Repeated validation preserves target numbers issued since restoration.
   A failed later gate can leave safe sequence gaps; no transaction rollback can
   justify rewinding setval.

For the synthetic CLI restore, configure `SEQUENCE_SOURCE_DB_HOST`, optional
`SEQUENCE_SOURCE_DB_PORT`/`SEQUENCE_SOURCE_DB_NAME`, and
`SEQUENCE_SOURCE_DB_PASSWORD_FILE` for the known original migrator connection.
The ordinary migration DB settings select the separate clean target. The source
must still be available to acquire/maintain the verified fence. CLI restore cannot
initialize a replacement authority or enable public ingress. Once the source fence
is released, its capability expires; any further write-admission review requires a
current fence or a separately approved durable production reservation authority.

A source restart/new epoch, source loss before trustworthy evidence, or loss of
that independent authority remains BLOCKED. This intentionally does not invent
an offline maximum from a surviving backup. The synthetic authority is a local
protocol model outside DB rollback, not independent production storage. Production
needs reviewed durable reservation/source-fencing continuity, authentication and
recovery across source loss/epochs. Existing RF offsite, journal, legal retention,
key custody, daily schedule and target RPO/RTO gates remain BLOCKED.

The actual PG16 regression now issues N, encrypts the real backup, issues N+1 in
the source AFTER it, creates source-fenced durable evidence, restores the old
archive into a new PG16/files volume and replays deletion commands. The first
new target nextval is >N+1. It also checks direct source nextval blocking, repeat
validation without rewinding, missing/bare/corrupted evidence, authentic wrong
source/epoch/fence nonce, stale/future timestamps, maxima below known evidence,
unbound old archives and a genuinely released source fence. New-version evidence
and measured timings are recorded after the new exact-HEAD CI run; no earlier
HEAD's green checks close this P1.

### P1-01 measured verification

The new dedicated [PG16 encrypted backup/restore job on implementation commit
79c02c2b6b8a8503614a622c51902afd0248f9d8](https://github.com/h1roshigoto/akbrizcase/actions/runs/42169746143/job/127734528672)
passed after the correction. This is new evidence, not the pre-review run:

| Measurement/check | Observed |
|---|---:|
| Backup / restore core | 0.438 s / 1.160 s |
| Snapshot → control-state interval | 7.497 s |
| Ciphertext / checked files and sidecars | 143612 bytes / 10 |
| FK / deletion replay | 30 / 6 |
| Number issued after backup not reused | PASS |
| High-water negative checks | 15 PASS |
| Real source nextval fenced / repeated validation forward only | PASS |
| Sequence witness is a production authority / public write admission | NO / NO |
| Production RF RPO/RTO, offsite, journal, legal policy, keys, schedule | BLOCKED |

`make backend-test backend-lint backend-types` also passed on that implementation
(1009 tests, format/lint and 216 strict-typed files). The complete six-job run on
the final publication HEAD and its measurements are linked in [PR #55](https://github.com/h1roshigoto/akbrizcase/pull/55).
This evidence-only documentation update changes no runtime code. Final acceptance
still requires all six jobs on the final exact HEAD; a partial or older green run
is not substituted for it. Source-fenced synthetic timing is not target RPO/RTO.
