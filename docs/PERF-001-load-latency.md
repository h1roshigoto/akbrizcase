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
