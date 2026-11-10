# MVP-048 — bounded load and operational smoke

`TARGET_LOAD=BLOCKED`, `PRODUCTION_ADMISSION=BLOCKED` until independently authorized
actual target evidence exists. This change does not approve MVP-049/050, real
users, expert content, legal collection, production ingress, merge or deployment.
The base is `2dce890929674a59ac32a4c7ae5ca9eb4fc85fda`.

## Reproduce

Use Python 3.12, Docker/Compose, sufficient disk and the pinned runtime images.
Run from a clean checkout. On Docker VFS allow substantially more storage than
image sizes suggest: every writable container layer is copied. The 30-source
profile normally uses Docker's copy-on-write storage. Never change daemon storage
or delete persistent volumes to make a benchmark pass.

```sh
cp .env.example .env
make init-secrets local-tls build
# Standard-library generator; no application credentials or DB access.
docker pull python:3.12.14-alpine
make mvp048-guards
make mvp048-smoke MVP048_ARGS='--report-dir /tmp/mvp048-evidence-NEW'
```

The report directory must be new/private (0700). Files are exclusive-created
0600, never overwrite a previous report. Without `--report-dir`, a private
system temporary directory is created. No reports, logs, mail or archives are
committed or uploaded. Only `MVP048_RESULT` allowlisted aggregates go to CI stdout.

For a shared NAT experiment with exactly one actual source:

```sh
make mvp048-smoke MVP048_ARGS='--profile single-ip-v1 --report-dir /tmp/mvp048-nat-NEW'
```

That profile has six independent sessions and one active request at a time. It
cannot establish the 200/30 requirement. Two measured new orders and six samples
per other operation are **INCONCLUSIVE**, even when functionally successful.
Use new projects for repeated runs; do not reset quotas or reuse their volumes.

The controller reuses the full MVP-046 HTTPS fixture: PostgreSQL **16.13-alpine**,
real FastAPI, Nginx, verified localhost TLS, staff authorization, domain publication
and actual ClamAV with a test-only signature database. Runtime application code,
security limits, migrations and business services are unchanged. The reduced
MVP-041 release Compose/modes are never used or modified. The PostgreSQL variant
is explicit synthetic infrastructure, recorded by actual image content ID.
Backend/frontend/PG/generator IDs and Alembic head accompany every report.

Existing `SyntheticStack` ownership/marker checks apply before seeding and
fault injection. Cleanup stops the owned stack and **retains its labelled
volumes**. Only newly created, ownership-checked generator containers are removed;
there is no volume deletion, prune, reset, migration downgrade or stamp.

## Profile and interpretation

`pilot200-v1` is a fixed, hashed profile, not caller-controlled numeric options:

| Property | Bound / behavior |
|---|---|
| Client pool | 200 independently owned sessions/tokens, round-robin across 30 sources |
| Network sources | 30 fresh, inspected container addresses on this project's internal web network |
| Active operations | At most 30 in measurement, one per source; concurrent replay is functional-only |
| Measurement | 15 seconds planned; actual interval and request time are reported |
| Request / controller deadline | 8 seconds / 900 seconds after stack preflight; process watchdog 1200 seconds |
| Per source | At most 240 HTTP attempts, 8 prepared sessions and 5 committed orders |
| Repeats / corpus | One pass; 54 items, 1 document/version/section, 1 published test tree |
| Warm-up | One button transition, two searches, one order per source; excluded from measured latency |
| Measured | 200 buttons, 200 knowledge searches, 200 catalog searches, 60 new orders |
| Functional | Concurrent new-key replay, stale version/revision/consent, ownership denial, real quotas and recovery |

Each source creates real sessions, starts published `SYNTHETIC035`, verifies its
allowed option `a`, then measures a button transition to its exact approved
result. Request UUIDs, `expected_version`, session ownership, consent version,
item revision and 64-hex request tokens use the existing HTTP contracts. Search
must find the exact published synthetic section through PostgreSQL FTS; catalog
search separately checks its expected 50-item subset. New-order measurement
includes full response validation after HTTP commit. The one concurrent replay
pair on source 0 must return one 201 and one 200 with identical receipts.

The controller reconciles **every** resulting order, consent version, product
snapshot, initial status history and exactly one manager-notification outbox row.
There are 92 committed orders for the full profile (90 warm-up/measured plus two
functional creations); 5 for the one-IP profile. Negative requests must leave
these totals unchanged. Outbox delivery never changes the commercial graph.

Nginx remains 120/min/IP, burst 119. PostgreSQL remains sessions 10/hour/IP,
orders 5/hour/IP and messages 30/min/session, using commit-scoped counters.
Source 0 saturates real quotas with HTTP, checks `Retry-After`, spoofed forwarding
headers, replay at exhausted creation quota, then waits for actual message-window
expiry. A separate bounded edge burst observes Nginx 429 and elapsed-time recovery.
No quota rows, server clocks, retry timestamps or limits are modified. Expected
429s remain visible in the functional ledger and cannot count as business success.
The hour-long creation quotas are not reset or claimed recovered in this smoke.

All source processes share the Docker host's monotonic clock. A sweep of actual
HTTP start/end intervals reports peak, time-weighted mean, active-operation
seconds and span. Equal-time completion precedes a new start. Clients are paced
across sessions: **30 sources/tasks do not imply 30 continuously busy requests**.
Report the observed peak and mean, including think/idle intervals. A target gate
requires an observed peak of 30; lower concurrency cannot be relabelled a pass.
This is a short bounded burst profile, not a sustained capacity/SLA proof.

Latency uses nearest rank `ceil(p*N)` for p50/p95/p99 and full client monotonic
HTTP time including body read and validation, with normal TLS verification.
Every attempt appears in all-attempt statistics; successful responses also have
an explicitly named distribution. Errors/timeouts are not hidden by computing a
successful subset. Unexpected 4xx/5xx, invalid responses and timeouts fail gates.
The minimum is **20 successful samples per operation**: at least one observation
occupies the upper 5% tail. This is a deterministic smoke floor, not a confidence
interval or statistical assertion about production. The main profile exceeds it.
Insufficient samples are INCONCLUSIVE; interrupted/dirty runs never publish PASS.
Handler event latency is a different measurement from client HTTP latency.

## Real operational recovery

Only the freshly owned synthetic project can enter fault injection:

1. The SMTP sink is stopped while HTTP orders commit; the resident worker is
   quiesced to observe atomic pending rows. A normal worker tick against the
   unavailable sink records `SMTP_UNAVAILABLE`, attempt 1 and a real +300-second
   `next_attempt_at`. No contacts or messages are exported.
2. After sink recovery, another process claims a due row, actually sends SMTP,
   then exits without finishing. The restarted resident worker waits out its real
   120-second lease, records `LEASE_EXPIRED`, waits the real 300-second retry, and
   drains the backlog. No queue SQL repairs, forced due dates or false sent rows.
   SMTP acceptance before crash can produce duplicate mail: **not exactly once**.
3. The owned PostgreSQL container is stopped. Liveness remains 200, readiness and
   real catalog/order HTTP return safe 503, then reconnect/readiness recover after
   restart. Committed graph totals and snapshots remain intact. Target attachment
   cannot invoke this restart or any SMTP fault injection.
4. A sampler records host CPU/RAM/vCPU/disk, protected files-volume disk, app-container CPU/RAM/OOM, PG current
   and active connections/max_connections/pool limits, outbox counts/attempts/oldest
   pending. Measurements have extrema and availability, not fabricated zeros.
   Private evidence also preserves closed resource samples with monotonic offsets
   and their clock origin for correlation with the HTTP ledger. Host-checkout
   and protected files-volume disk readings remain distinct. Missing/low volume
   observations cannot borrow a successful host-disk result.
   Collection overhead is part of this smoke; peaks between samples may be missed.
5. `app.core.operations` authorization and protected aggregates are reused. Missing
   backup receipts/runtime sources remain unavailable/BLOCKED. Synthetic backup
   rehearsal does not establish offsite custody, RPO<=24h or RTO<=4h. Disk low/error
   paths use the existing monkeypatch tests, never intentional disk filling.

An ordinary recovery takes at least seven minutes after the crash and remains
bounded by real deadlines. The controller must not accelerate that schedule to
fit CI. The added CI job has a 25-minute budget including image builds; all eight
existing mandatory jobs and their timeouts/security checks remain intact.

## Authorized target measurement (no deploy)

Without authorization, this command returns BLOCKED before opening a socket:

```sh
make mvp048-target-preflight
```

A responsible infrastructure owner must **first prepare and independently
approve** a full, empty synthetic staging on the actual target host. The command
below only attaches to existing resources; it does not deploy, migrate, seed,
start/stop the app, change modes, restart PG or inject infrastructure faults:

```sh
python3 scripts/mvp048_smoke.py target --authorization /protected/mvp048-authorization.json \
  --report-dir /protected/mvp048-measurement-NEW
```

Use an individual protected terminal; active admin credentials use non-echoing
prompts, never packet/argv/env/history. The adapter deliberately accepts only this
repository's complete disposable-profile topology (`akbrizcase-https-test-` plus
10 hex digits), owned labelled volumes, internal web network and verified
`https://localhost:PORT` on the target host. Arbitrary URLs, production projects,
shared mounts and the reduced release profile cannot qualify. The operator
quiesces the staging worker before measurement and restores it afterwards;
automation does not stop that pre-existing worker or clean up its stack.

The private regular authorization file must be owned by the invoking operator,
0600, <=8192 bytes, without symlinks/duplicate/unknown fields. Its exact schema:

```json
{
  "schema": "MVP048_AUTHORIZATION/v1",
  "head": "FULL_40_HEX_REVIEWED_HEAD",
  "profile_digest": "SHA256_OF_FIXED_pilot200-v1_PROFILE",
  "host_identity_sha256": "SHA256_OF_EXACT_/etc/machine-id_BYTES",
  "operator_reference_sha256": "SHA256_OF_INDEPENDENT_APPROVAL_REFERENCE",
  "expires_at": "AWARE_ISO_TIMESTAMP_WITHIN_NEXT_TWO_HOURS",
  "project": "akbrizcase-https-test-0123456789",
  "images": {
    "backend": "sha256:CONTENT_ID", "frontend": "sha256:CONTENT_ID",
    "postgres": "sha256:CONTENT_ID", "generator": "sha256:CONTENT_ID"
  },
  "origin": "https://localhost:8443",
  "ca_file": "/protected/ca.crt",
  "dataset_sha256": "SHA256_OF_OPERATOR_REVIEWED_CANONICAL_DATASET",
  "synthetic_only": true,
  "ssd_attested": true,
  "backup_receipt": "/protected/backup-receipt-inside-api.json",
  "runtime_journal": "/protected/runtime-journal-inside-api.jsonl"
}
```

This is a **schema illustration**, not usable approval. Backend/frontend images
must carry OCI `org.opencontainers.image.revision` matching the clean exact HEAD;
all running image IDs must match. Nginx policy must equal the checked-in HTTPS
policy. PG16/schema, empty commercial/chat/quota tables, synthetic document text,
corpus cardinalities, protected admin authorization, TLS, readiness, collection
and published diagnostic scenario are verified before mutating HTTP load.

`dataset_sha256` binds the exact single-line `psql -At` JSONB aggregate used in
`mvp048_target.attach`: ordered complete rows of catalog_items, equipment_models,
documents, document_versions, document_sections, diag_trees, consent_texts and
consent_publications. The owner inspects those contents in the protected staging
context and records its digest in independent approval. Do not approve a digest
without confirming all content is synthetic; a DB marker alone is insufficient.
The digest/rows are never printed by the runner. Fixed profile digest:

```sh
PYTHONPATH=scripts python3 -c 'from mvp048_result import PROFILES; print(PROFILES["pilot200-v1"].digest)'
```

Target PASS additionally requires sufficient successful samples, all thresholds,
actual 200 clients/30 source addresses/30 peak overlapping operations, observed
resources, non-low disk, SSD attestation, current protected runtime/backup sources.
Any uncertainty blocks it. Actual SMTP/DB fault recovery on a pre-existing target
remains NOT RUN/BLOCKED without a separately scoped operator procedure; the local
recovery result cannot promote it. No actual target was supplied for this task.

## Evidence and regression matrix

`MVP048_RESULT/v1` is closed and checked before publication. It binds exact HEAD,
dirty state, fixed profile hash, immutable image identities, schema, corpus,
network cardinalities, actual overlap, phase counts, status totals, timeouts,
RPS/distributions, resource availability and separate outcomes. Before emitting,
statistics/overlap are rebuilt from the private ledger; unknown fields, numeric
bounds, inconsistent totals, stale HEAD, impossible percentiles, modified profiles
and invented gates are rejected. Public output has no IPs, URLs/query text,
contacts, cookies, chat/request tokens, HMAC, mail, private paths or SourceRef.
A private directory path is never included in the public aggregate.

| Requirement | Actual executable coverage |
|---|---|
| Percentiles, limits, overlap, warm-up, insufficient samples, errors/timeouts, incomplete children, privacy/schema/profile/HEAD | `make mvp048-guards` |
| Missing/stale/invalid target authorization, wrong host/profile/TLS/images, no accidental remote/deploy | target guards + read-only live target preflight |
| Real buttons, both searches, orders, concurrent replay, stale consent/revision/state, ownership, quotas/spoofing, DB graph | `make mvp048-smoke` through PG16/Nginx/verified TLS |
| SMTP unavailable, real retry/lease/crash/restart/backlog; DB 503/reconnect/readiness | `make mvp048-smoke` |
| Low/unavailable disk; missing/failure/stale/invalid backup receipt; protected aggregates/ACL | existing `test_observability.py`, `test_safe_operations.py`, full backend/PG suites |
| Full ACL/publication, contracts, competing writes, rollback, worker failed/manual retry | existing PG16 regression suites, unchanged |
| Encrypted clean restore / offsite distinction | existing mandatory Encrypted backup/restore job, unchanged |
| Linked browser, frontend/auth/CSRF/security regression | existing mandatory Linked browser E2E and Compose jobs, unchanged |

Executed commands, exact-HEAD Actions run/jobs, local observed numbers and
limitations accompany the PR delivery. No stored run is a permanent performance
guarantee. REAL_EXPERT_CORPUS, REAL_TOP5_ACCEPTANCE, TARGET_STAGING and
PRODUCTION_ADMISSION require their existing independent decisions and evidence.
