# PERF-001 — load latency investigation

Base: `daaf30b427a29d0e9e9155030ab1fbb08468f6d7`, post-merge CI
[43076987813](https://github.com/h1roshigoto/akbrizcase/actions/runs/43076987813).

The baseline's closed `MVP048_RESULT/v2` reports button p95 926.054397 ms
(limit 300, FAIL), order 1266.068097 ms (1000, FAIL), catalog 407.264368 ms
(800, PASS), knowledge 511.395379 ms (800, PASS). All nine jobs succeeded;
SYNTHETIC_SMOKE=PASS is functional/operational evidence, not performance PASS.
200 clients, 30 sources, peak 30, mean active 16.35645; no errors/timeouts.
The 660 measured attempts include full verified TLS/body/validation time.

## Investigation procedure

`perf001_probe.py` is piped into the freshly owned synthetic API container
AFTER the existing measured and functional HTTP phases and graph reconciliation.
It cannot run in the target branch. It verifies demo DB marker, APP_ENV, PG16 and
specific synthetic corpus identities. Every service write uses rollback, and
counts of orders/consents/snapshots/history/outbox/chat/messages/quotas must match.
Sequence allocation is nontransactional and can advance; no committed order is
added. No runtime limits, settings, clocks, quotas or worker states are changed.

The probe reports wall/thread CPU, cursor execution time/count, selected function
cumulative times, numeric EXPLAIN ANALYZE tree statistics, and a controlled
same-item competing-order experiment. SQL, parameters and complete plans stay
in memory. Function times overlap and must not be summed. Cursor time includes
network/server/lock waits; it is not pure SQL CPU. Thread CPU excludes work in
other threads/processes. The controlled lock experiment is not HTTP p95 evidence.
A 500 ms observation window precedes release, below the unchanged 3 s lock timeout.
All probe results are separate from the unchanged MVP048_RESULT/v2.

Candidates, pending measurements: exclusive catalog locks serialize independent
orders; repeated tree/dependency reads, SQL build/execute and projection can
contribute to diagnostic latency; the 10-connection pool can transmit contention
to other operations. Baseline resource extrema are sparse and not phase-bound:
11 PG connections, 3 active; summed container CPU max 113.99%, host max 41.33%
on 4 vCPU. They do not establish pool saturation or a CPU/IO bottleneck.
The resource sampler starts an authenticated Python process (including Argon2)
every cycle; this is measurement overhead, not anonymous request authentication.
No causal share is assigned to HTTP/TLS, Argon2 or scheduling without evidence.

## Status

Investigation in progress. Both failing performance gates remain FAIL until
fresh exact-HEAD evidence satisfies their existing thresholds. Shared CI runners
and a short synthetic corpus cannot establish production capacity.

FULL_MVP_READINESS=BLOCKED
REDUCED_PILOT_READINESS=BLOCKED
PUBLIC_LAUNCH_RECOMMENDATION=NO-GO
HUMAN_GO_NO_GO_DECISION=PENDING
PRODUCTION_ADMISSION=BLOCKED

## Measured findings and focused changes

Investigation run [43085198681](https://github.com/h1roshigoto/akbrizcase/actions/runs/43085198681)
uses HEAD `094891754e5d3d017d4cb4e3ccbfb4faa9f2c26a`. Its backend context
`71e3a49a89809dba30e4602e49dd3e3895570494` is IDENTICAL to baseline; the
probe runs after all measured HTTP. Button/order p95 were 1007.44/1691.82 ms;
catalog/knowledge 459.89/594.17 ms, still FAIL/FAIL/PASS/PASS. This repeat
shows shared-runner variation; it does not attribute that variation to a cause.

The separate PG16 probe measured:

| Scope | Wall | Thread CPU | Cursor time | Statements |
| --- | ---: | ---: | ---: | ---: |
| Order service + rollback | 47.04 ms | 37.67 ms | 14.39 ms | 32 |
| Session + diagnosis start + button + projection + rollback | 97.98 ms | 75.12 ms | 30.09 ms | 43 |

These are single profiled, isolated service observations, NOT endpoint p95 or
additive partitions. The diagnostic probe reads its own uncommitted writes and
does not model the HTTP route's separate post-commit projection transaction.
Tree EXPLAIN ANALYZE: execution 0.400–0.433 ms, planning 1.874–1.962 ms,
cost 98.42–106.26, JIT 0 ms, shared reads 0, hits 24. This does not support a
slow tree SQL execution/JIT/disk explanation for this tiny corpus.

The same-item order experiment observed `pg_stat_activity.wait_event_type=Lock`
and a nonempty `pg_blocking_pids`, with the competing order unable to finish
while the first held its transaction. It finished in 525.46 ms after the 500 ms
observation window released the holder. This proves unnecessary serialization
at the existing exclusive catalog read lock; it does not quantify its entire
share of the HTTP tail. Change: `FOR SHARE` allows concurrent order snapshots
while preserving conflict with catalog UPDATE/DELETE through commit. All consent
rechecks, UNIQUE replay/rollback, snapshots, outbox and quota checks are retained.
The existing two-direction catalog-writer test still observes a real lock wait.
A new PG16 test holds the first complete write set, requires the second to
commit, then verifies both successful and rolled-back holders, receipt replay,
wrong-token/stale-revision denial, graph totals and committed quota only.

Repeated SQL-expression construction is measured independently on local Python
3.12.14/SQLAlchemy 2.0.46 with an execute stub returning no row: 1000 hot pin
lookups cost 675.226 ms thread CPU on original source versus 0.382 ms with
statement reuse. This deliberately excludes DB, SQL compilation and rendering;
it is NOT an HTTP performance claim. Change: memoize only the two immutable
SELECT shapes (key/pin), using a per-execution bound lookup. No tree, SourceRef,
publication/ACL decision, graph or response is cached. Every read still executes
the complete predicates; graph validation and the post-commit snapshot remain.
Parallel bound-lookup tests and a real warm-statement ACL-change/rollback test
cover identifier isolation and fresh authorization; existing retired/revoked,
replay, competing-write, denied and commit-failure suites remain mandatory.

Argon2 is absent from these public service paths; SHA/HMAC remains unchanged.
The authenticated resource sampler still incurs Argon2 work. A separate local
five-verify microbenchmark had median 87.126 ms (79.730–101.986 ms), with no
claim about this work's share on CI. No security work or sampler was removed.
Pool occupancy/wait, per-request pipeline queue time and HTTP/TLS components
have not been isolated under load. CPU/lock effects can propagate to other
operations through the shared pool, but that causal share remains unmeasured.
The thresholds, concurrency, sample selection and end-to-end timer are unchanged.

Final exact-HEAD nine-job status and actual before/after MVP048_RESULT/v2 values
are recorded in the PR after CI completes. Until then performance remains FAIL.

## Under-load phase investigation (continuation)

The continuation adds opt-in `PERF001_TIMING=1` only to the full, freshly owned
MVP-048 synthetic API service. Ordinary runtime and the target adapter do not
enable it. No new endpoint, response header, privilege or security exemption is
introduced. There is no further optimization pending these measurements.

The original client sample object, monotonic HTTP timer (through body validation
and close), fixed profile, percentile/status handling, quotas and deadlines stay
unchanged. Additional timestamps are side measurements; their overhead is part
of the actual HTTP result and cannot be subtracted from a gate.

The outer ASGI observer and context propagated to the existing AnyIO threadpool
collect a closed numeric `HTTP_PHASES` event. The only correlation value is the
already existing server-generated request UUID. Events never contain SQL or
parameters, URL/path/query text, payloads, headers, contacts, credentials, chat or
order tokens. Normal request/error handling, commit/rollback and projection are
unchanged. The log formatter validates the nested metric keys and numeric types.
These safe runtime events remain private; they are not uploaded as artifacts.

After measurement the controller joins all 660 client records to server events
and exports **only aggregate** `PERF001_PHASES/v1`, bound to exact HEAD. The join
is reconciled against the unchanged all-attempt ledger. Missing/duplicate server
records, unknown fields, damaged clocks and tampering cannot masquerade as full
coverage. Failed/unmatched attempts remain counted and visible in the all-attempt
HTTP distribution and in p95-cohort coverage. Request UUIDs and absolute clocks
are absent from this public aggregate.

Measurements and limits of interpretation:

- `pool_queue_ms` times SQLAlchemy's original Queue.get, including mutex/condition
  waiting and scheduling. `pool_acquire_ms` covers original QueuePool._do_get;
  `connect_ms` is a nested physical connection creation span. Only thin subclasses
  delegate to `super`; no capacity, deadline or connection algorithm is copied.
  Reentrant acquisition is counted once. `checkout_after_get_ms` covers pre-ping,
  checkout bookkeeping and scheduling after get. None of these overlapping
  percentiles may be summed. Existing pool stays 5 + 5, timeout 3 seconds.
- `dispatch_ms` measures submission-to-worker-start using the original threadpool
  function/limiter. Worker wall and thread CPU, observed token counts, cursor wall/
  thread CPU, lock-capable statement time, commit/rollback and connection-hold
  time are distinct. Lock-capable SQL time is **not** pure PostgreSQL lock wait.
  Commit-nested SQL/acquisition spans are identified to avoid double counting.
- Chat write and fresh post-commit projection retain their separate UoWs; both
  commits and the projection's commit are timed. No stored response is trusted
  and no projection, ACL or state validation is skipped.
- The outer ASGI-to-error-middleware gap observes framework scheduling. Route
  entry, dispatch, worker completion and response send span the request pipeline.
  Client socket/DNS+TCP, verified TLS, headers wait, body read, validation and close
  are measured without extra HTTP operations. The signed send-to-ASGI residual
  includes network/proxy/Uvicorn admission and sender/receiver overlap; it must
  not be relabelled as a pure server queue.
- Per-operation component distributions also report the **same requests at or
  above actual client p95**, not independently selected per-component tails.
  API process CPU is a counter delta over the correlated request envelope,
  including framework/logging/worker CPU; per-request process deltas are not
  added. Thread CPU excludes other threads and is not an additive wall partition.
- Existing resource sampler intervals and its unchanged authenticated probe are
  observed. Argon2 verification wall/process CPU is timed without touching its
  parameters or checks. Sampler overlap is correlation, not evidence of causality.
- Numeric PG16 pg_stat_database deltas bracket measurement before functional
  tests. They include the collector/sampler, possible stats flush lag, planning
  and server waits; active_time is server wall activity, **not PostgreSQL CPU**.
  No pg_stat_reset, forced flush, server setting, extension or SQL logging is used.
  Disabled IO timing is unavailable, never represented as a measured zero.

Unit tests cover privacy canaries, context isolation, exceptions, pool capacity/
release/timeout, ledger and tail reconciliation and incomplete coverage. New
PG16 cases cover the real ten-connection cap, commit/flush rollback, denied/replay
without extra business rows, and separate post-commit chat projection. They run
only in the existing CI integration job. Actual measurements and conclusions are
recorded in PR #68 after the exact-HEAD nine-job run; until then no bottleneck is
claimed proved by this instrumentation.
