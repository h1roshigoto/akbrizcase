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
