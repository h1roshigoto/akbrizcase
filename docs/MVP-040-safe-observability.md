# MVP-040: safe logs, audit and operator signals

Implemented from `76e2008e01e51651e3a0456afbef1478fc90d07d` (MVP-039,
merged PR #52). Synthetic verification only; PR stays Draft for independent
review. MVP-037/038 production release gates remain blocked; ADR-038-A remains
PROPOSED. No MVP-041/042, deploy, backup implementation or retention change.

## Runtime event contract

Standard Python logging writes one JSON event per line. Closed fields:
`event_code` (fixed enum), server UTC `at`, server-generated UUID `request_id`,
HTTP `status`, bounded `endpoint` category, `duration_ms`, and integer counters
`processed/skipped/errors/manual_review`. Unknown fields/values are rejected.
There are no customer/entity IDs in runtime diagnostics. Endpoint categories:
health, readiness, admin, catalog, chat, orders, knowledge, other. Categories
never include route arguments or URLs. HTTP events include handled errors and
unexpected failures; request IDs, no-store, CSP and error envelopes are preserved.
Unexpected exceptions receive a fixed INTERNAL_ERROR envelope, without traceback.

DB/SMTP/files/worker/cleanup failures use fixed events, without exception text,
SQL parameters, paths, payloads, query strings, IPs, headers or free reasons.
SMTP/outbox still retains its existing normalized error-code allowlist. HTTP 5xx
counts are distinct from DB/SMTP/cleanup event counts; do not add them together
as distinct incidents. HTTP 4xx includes validation/auth/quota outcomes, not
necessarily technical failures. Runtime events describe response headers/handler
latency, not completion of a streamed transfer or end-to-end browser latency.
Uvicorn access/error and SQLAlchemy/psycopg/HTTP client diagnostic loggers are
suppressed; application boundaries supply the technical signals. A closed root
formatter replaces other dependency messages with RUNTIME_LOG_SUPPRESSED and
never formats their exception or extra payload. Nginx already disables access
logs and limits error verbosity; PostgreSQL retains the existing statement-log
restrictions. Do not enable DEBUG/SQL echo/SMTP debug/access logs operationally.

## Audit and minimized chat

New AuditEvent ORM inserts validate action/entity/result and per-event keys and
value types. The same guard covers staff/security, order status, notification
retry, catalog writes, document/tree lifecycle, review/publication/flag changes,
protected privacy operations and `operations.observe`. Forbidden payloads fail
with fixed AUDIT_SCHEMA_INVALID; they are not silently dropped. Unknown actions,
arbitrary JSON, nested objects, free reasons, body/response/logins/tokens are not
accepted. Invalidation `reason` is a closed technical enum. UUIDs, checksums,
opaque approval references, timestamps, booleans, revisions and fixed issue-code
lists remain purpose-bound evidence. Existing expert attribution is deliberately
preserved in evidence events only, with the existing expert-name validation;
it is personal data, not anonymous telemetry. See `audit_schema.py` for exact
schemas. Existing historical rows are not rewritten. DB ACL and audit maintenance
remain unchanged; direct privileged SQL is outside the application write guard.
Retention tests create explicitly historical rows using SQL to prove that even
old/unknown metadata is still preserved and never exported by maintenance.

The existing `minimized_input()` also masks narrow no-break spaces and common
Unicode phone dashes. Original request_hash, ownership/version checks and routing
continue using the original input. Stored text after masking remains potentially
personal data and is not exported for a regression corpus.

## Protected operator report

Run in the existing API container from an authorized terminal:

```sh
docker compose exec api python -m app.core.operations
```

Individual admin login/password use non-echoing input. Authorization is checked
against the DB with the existing staff lock; disabled staff/manager/invalid
credentials cannot read any aggregate or filesystem/backup/runtime report. A
fixed operations.observe audit records the attempt, without credentials. Runtime
DB credentials suffice; no public endpoint, extra service or DB privilege is added.
DB unavailable/misconfigured/incompatible returns only a fixed failure signal.
A successful report command exits 0; inspect its signal/status fields for alerts.

Optional operator-owned input files, readable only in the operator context:

```sh
python -m app.core.operations --runtime-journal /protected/runtime.jsonl \
  --backup-receipt /protected/backup-check.json
```

The runtime journal must contain only the application's JSON event lines (no
Compose prefixes), at most 8 MiB / 50,000 events. The operator selects the source
processes/time range; use a protected file, include API and worker, and retain no
more than the existing technical log policy permits (proposed 14 days, unchanged).
Report counts/latencies cover events in the last 24 hours of that selected sample;
coverage is explicitly `operator_selected_sample`, not a durable/global counter.
Absent/stale/invalid/oversized sources are unavailable; malformed data is never
echoed. No journal file is read before operator authorization. No new log storage
or automatic collection pipeline is introduced.

| Metric | Source / meaning |
|---|---|
| Orders by kind/status and count | Fixed SQL COUNT/GROUP BY, currently retained rows |
| Chat sessions/outcomes | Stored routing state only; resolved/handoff are not sales, satisfaction or confirmed resolution |
| Usefulness / conversion | unavailable: no explicit feedback or sales source exists |
| HTTP request/error/status counts and mean/p95 handler latency | Optional bounded runtime event sample; p95 nearest rank |
| PostgreSQL connections | COUNT(pg_stat_activity) in current database; includes reporting connection, no query/session contents |
| Outbox counts, oldest pending age, delivery-error rows | Fixed SQL aggregates; empty queue age null; oldest >24h, failed and stored error generate signals |
| Worker/SMTP/DB/cleanup errors | Fixed runtime failure counts; missing observations unavailable, never healthy by assumption |
| Disk free/total | disk_usage on APP_FILES_DIR; low below 100 MiB or 5%, unavailable on OS failure |
| Backup verification | Optional operator receipt only; absent source not_configured, missing/invalid expected receipt unavailable |

The backup receipt is a regular file <=4096 bytes, with exactly
`{"status":"success"|"failure","checked_at":"aware ISO timestamp"}`. No payload,
backup path, log, customer ID or arbitrary fields are accepted. Failed receipts
produce BACKUP_FAILURE; success older than 24h produces
BACKUP_CONFIRMATION_STALE. This is an external check confirmation, not proof of
backup/restore or compliance with RPO/RTO. The backup producer/process is MVP-042
and is not created here. Symlinks, nonregular files and future timestamps fail
closed; no backup is presumed configured or successful.

## Verification and limits

Canaries use synthetic email, phone, chat/request tokens, Authorization, cookie,
query q, free reason, path and hostile text. Tests cover HTTP validation/domain/
SQL/OS/unexpected errors, structured formatting, audit rejection, contact masking,
original hashing, latency counts, receipt failure/missing/stale states, simulated
low/unavailable disk, PostgreSQL aggregates/ACL, SMTP failure, stale/failed outbox,
cleanup errors and real PostgreSQL connection refusal. Disk failure injection is
read-only; the environment's actual disk exhaustion during Docker build is also
reported in the task evidence. No filesystem filling test is introduced.

Required regression/PG16/Compose/HTTPS and exact-HEAD GitHub Actions results are
reported in the PR/task after execution. Missing sources and environmental
failures are never marked passed. No legal retention binding, audit deletion
permission, feedback inference, backup scheduling or release gate is added.
