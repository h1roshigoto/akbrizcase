# MVP-044 — Commercial regression on PostgreSQL 16

## 1. Baseline, HEAD and scope

Base: `39bc53d48da4b29742976dc9ac75a9218a22d505`, verified against
`origin/main` and GitHub before branching. Branch: `mvp-044-commercial-regression`.
The final tested HEAD and Actions links belong in [Draft PR #57](https://github.com/h1roshigoto/akbrizcase/pull/57) to
avoid a self-referential commit. Synthetic data only; no merge or deployment.

## 2. Required checks (implementation plan §8)

| Check | New linked evidence | Result |
|---|---|---|
| 1. Double/parallel POST, one graph | Creation contention followed by worker/status/privacy operations | PASS, PG16 |
| 2. AQ-01 erasure/replay | Witnessed erasure after catalog/status/SMTP changes, hidden response matrix | PASS, PG16 |
| 3. SMTP failure preserves order | Failure exhaustion → protected retry → real SMTP acceptance → erasure | PASS, PG16 |
| 4. Immutable snapshots | Catalog service edits/archives after HTTP creation, stale selection conflict | PASS, PG16 |
| 5. Support comment survives chat cleanup | HTTP chat/support creation → journal cleanup → manager read → erasure | PASS, PG16 |

## 3. Existing coverage and the new boundary

This map was made before adding the suite. Existing cases remain in their
original suites; MVP-044 must execute its own linked scenarios.

| Existing source | Already covered | New cross-module boundary |
|---|---|---|
| `test_order_creation.py` | HTTP write set, sums, snapshots, AQ-01, UNIQUE, rollback, canonical array order | Same accepted graph through catalog service, staff status, SMTP and journal |
| `test_order_reads.py` | Protected projections, literal text, direct chat DELETE | Actual witnessed chat cleanup before manager read and later PII erasure |
| `test_order_status.py` | Allowed/disallowed transitions, stale version, audit failures, two managers | Transitions on HTTP-created mixed/service/support graphs with notification/privacy checkpoints |
| `test_email_worker.py` | SMTP wire/retry/crash, lease recovery and stale CAS | Worker after catalog/status changes, then protected manual retry and journal boundary |
| `test_notification_retry.py` | Strict protected retry form, failed-only, atomic audit, race | Retry in the same end-to-end business graph and neighbor isolation |
| `test_cleanup.py` | Retention, policy pinning, PII erasure, chat batches, CHECK/FK | Public replay matrix and protected support detail after real cleanup |
| `test_deletion_reconciliation.py` | PREPARED/witness/APPLIED, rollback/lost ack, restore simulation | HTTP-created neighboring commercial graphs, full-row postconditions after faults |
| `test_privacy.py` | Publication gates and ordering, immutable evidence | Creation/publication contention with existing order/outbox/history preserved |
| `test_catalog_applicability.py` | Publication/revoke/revision and old snapshots | Commercial edits/archive through the permitted catalog write service |

Canonical array order is significant (MVP-009/010 and API contracts). Reordering
is a body conflict, not an equivalent request. MVP-039 also defines saturated
quota behavior: hidden/unused keys return the same 429 when capacity is exhausted;
AQ-01 404 checks therefore run with available capacity. Historical MVP reports
name earlier schema heads; the current implemented head is
`0011_mvp039_http_security`. Neither discrepancy requires a contract change.

## 4. Fault injection

The suite uses scoped engine/Session listeners with teardown in `finally` and
PostgreSQL `SELECT 1/0` after actual writes or at outer commit. Hooks do not replace
domain services. `commercial_helpers.same` hides its traceback to keep complete
private comparisons out of pytest diagnostics. No raw business rows are printed.

| Fault boundary | Expected | Observed / executable evidence |
|---|---|---|
| Consent INSERT | No accepted partial graph or charged quota | PASS: `test_creation_fault_rolls_back_then_same_key_completes_lifecycle[consents]` |
| Order INSERT | Rollback order and consent, neighbor unchanged | PASS: same test `[orders]` |
| Snapshot INSERT | All or none, then same-key recovery | PASS: same test `[order_items]`; real PG error after snapshot write |
| Initial history INSERT | No order without initial history | PASS: same test `[order_status_history]` |
| Initial outbox INSERT | Entire mandatory write set rolls back | PASS: same test `[email_outbox]` |
| Creation commit before success | 503, zero partial writes, same key can create | PASS: same test `[commit]` |
| Lost creation commit acknowledgement | 503 is not proof of rollback; retry reconciles existing number/amounts | PASS: `test_creation_lost_commit_ack_reconciles_original_receipt`; actual successful UoW followed by injected transport loss, then catalog change and 200 |
| Status history/audit INSERT and commit | Whole status/history/audit transition rolls back | PASS: `test_status_fault_after_smtp_preserves_whole_graph[history/audit/commit]` |
| Lease commit | No SMTP after uncommitted claim | PASS: `test_worker_fault_after_status_and_catalog_preserves_graph[lease_commit]` |
| SMTP result commit | SMTP accepted but DB remains recoverable sending | PASS: same test `[result_commit]`; recovery, duplicate with stable Message-ID |
| Worker crash before/after acceptance | Recover lease; duplicate permitted only after acceptance | PASS: same test `[before_smtp/after_smtp]`, actual child `os._exit(77)` |
| SMTP timeout | Allowed pending/backoff; graph unchanged | PASS: same test `[timeout]`, real loopback SMTP sink with bounded transport timeout |
| Result CAS | Old lease cannot acknowledge either success or failure | PASS: `test_two_workers_lease_cas_and_protected_retry_states` |
| Manual retry audit/commit | No partial failed→pending | PASS: `test_manual_retry_fault_after_catalog_and_status[audit/commit]` |
| Journal filesystem export | No mutation without durable witness | PASS: `test_journal_faults_after_http_status_and_smtp[export]`, real absent sink directory |
| Journal apply/commit | Witness retained, full rollback; later same command succeeds | PASS: same test `[apply/commit]` |
| Journal lost acknowledgement | Fresh postcondition confirms APPLIED; exact retry adds no effect | PASS: same test `[lost_ack]`, exception after successful commit |
| Chat messages/session delete and commit | Rollback messages, FK and support graph together | PASS: `test_support_chat_cleanup_fault_manager_read_and_later_erasure[messages/session/commit]` |

The commit acknowledgement seams model process/transport loss after an actual
successful COMMIT. They do not simulate a production network partition. SMTP is
at-least-once: duplicate mail with the same Message-ID is not a second order.

## 5. Concurrency

Each parameter `iteration=0,1,2` is an unconditional fresh-fixture execution,
not retry-until-success. One full run executes **33 race cases**:

| Scenario | Cases per full run | Synchronization |
|---|---:|---|
| Same key, original/different tokens | 6 | Hold real order INSERT; observe independent PostgreSQL blocker; UNIQUE loser rolls back |
| Distinct keys, unique numbers and full quota | 3 | Two HTTP submissions; controlled contention on catalog locks |
| Creation vs catalog/publication, both winners | 12 | Held INSERT or permitted write-service transaction; observed row/advisory wait |
| Replay vs witnessed erasure, both orderings | 6 | Real order row lock; replay waits or cleanup SKIP LOCKED defers |
| Two staff, same expected_version | 3 | First UPDATE holds staff/order locks; second HTTP caller waits; 200/409 |
| Two workers and stale lease CAS | 3 | Independent backend PIDs, Barrier/Event, held lease transaction and SKIP LOCKED |

Lock observation uses `pg_locks` and `pg_blocking_pids`: runtime cannot inspect
the migrator's private `pg_stat_activity.wait_event_type`. Waits are bounded
(2-second observation, 3-second lock timeout, 5-second event/future bounds);
no random sleeps, arbitrary timeout inflation or automatic retry. Listeners,
connections and SMTP sockets/threads are scoped. Final repeated-run counts and
exact-HEAD Actions evidence are recorded in the Draft PR delivery report.

## 6. Constraints and business postconditions

The common helper reads a separate REPEATABLE READ snapshot of orders, items,
consents, history, outbox, chats/messages, audit, journal and quota buckets. It
checks relationships, exact initial history, history/version cardinality, sums
from all snapshots, NULL unknown amounts/delivery, support-without-items, erased
fields/reasons and chat linkage. Checkpoints compare complete relevant rows and
neighbor graphs. Sequential status checks additionally allow only the documented
state/timestamp fields and exactly one matching history/audit event. SMTP/retry
preserve the full business graph; outbox identity and creation time are retained.

Actual constraints/triggers preserved and exercised:

- `uq_orders_idempotency_key`, `uq_orders_number`, `uq_orders_consent_id`,
  `uq_order_items_order_id`, `uq_email_outbox_order_id`.
- `fk_orders_consent_id_consents` RESTRICT; order children CASCADE;
  `fk_orders_chat_session_id_chat_sessions` SET NULL.
- `ck_orders_support_comment`, `ck_orders_contact_evidence`,
  `ck_orders_erased_fields`, `ck_orders_delivery`, `ck_orders_totals`,
  `ck_orders_amounts`; `ck_email_outbox_lease`, `ck_email_outbox_sent_at`,
  `ck_email_outbox_failed_at`, status/attempt/error constraints.
- `order_item_immutable`, `order_identity_immutable`,
  `consent_evidence_immutable`, `consent_text_immutable`,
  `consent_publication_immutable`; deferred `erased_reasons`;
  `deletion_entry_immutable`, `deletion_basis_immutable` and journal policy FKs.

The damaged-restore test copies persisted rows into a separate synthetic graph
with absent/corrupt historical publication. It inserts fixture evidence without
disabling triggers or rewriting an existing consent. Dependent eligibility fails
closed despite a newer valid publication. Manual full deletion uses the existing
authenticated migrator procedure and explicit synthetic decision UUID; it removes
only the target graph/consent. Neighbors, publications and independent audit stay.

AQ-01: erased retained rows return the same 404 for original/wrong/alternative
well-formed tokens and identical/changed bodies, with no post-erasure 410, new
verification material or new children. Missing/malformed headers remain 422.
Response comparison covers envelopes and headers, normalizing only request IDs
and CSP nonces. No public GET reveals the order. These checks run with available
quota; MVP-039's full-quota hidden/unused 429 rule remains normative. Matching
responses do not establish constant-time behavior; timing side channels are not
measured by MVP-044 and remain separately NOT PROVEN.

## 7. Defects

No domain defect found in the executed new scenarios. One existing harness defect
was found during ENOSPC recovery: the tests service lacked the integration label,
so `recover_for_stop` correctly refused its interrupted containers. The tests
service now carries the same label as its isolated PostgreSQL; recovery guards
are unchanged. Old unlabelled containers from this run were individually verified
by exact ID/project/service/image before stopping; volumes were preserved.
Harness issues encountered
and corrected during development: raw-driver fault escaping SQLAlchemy's error
classification, missing test imports, observing a migrator through restricted
`pg_stat_activity`, and attempting to edit immutable consent fixture evidence.
The final tests use wrapped real PG errors, cross-role lock observation and a
separate synthetic restored graph. No production service/schema/security change.

## 8. Commands and CI

```sh
make commercial-regression
make integration                 # includes a separate fresh commercial suite
make backend-test backend-lint backend-types
make acceptance-guards
```

`scripts/commercial_result.py` requires all **64 executed PASSED cases** across
the exact 16 groups, matching collection and summary totals, with no duplicates,
skip, xfail, xpass or deselection. Collection alone fails. Its three negative/
positive guard tests are included in `acceptance-guards` (45 tests passed).
The safe JSON includes actual Git HEAD and dirty-worktree flag; Actions also
requires equality with `COMMERCIAL_EXPECTED_HEAD`.

Local new suite: **64 passed in 73.41 s**, PostgreSQL **16.13 Alpine**, guarded
project `akbrizcase-dbtest-3949bd1e78ac`. This was a dirty implementation tree,
not exact-HEAD CI. The final implementation then passed **64/64 in 70.42 s**
at clean HEAD `c66ce2add47d37dccd26ac0fd7548e631828851f`, project
`akbrizcase-dbtest-9e13c29a2d9b`; this documentation update follows that checkpoint.
Each run executed all 33 race cases; two successful local full runs therefore
executed 66 race cases, with the final run including strengthened contention and
explicit lost-ack hook-reached assertions. Earlier failed development runs are
not counted as acceptance. Backend: **1009 passed**, Ruff/format and strict Mypy passed.
The full two-stack local PG regression was interrupted by ENOSPC during migrations
(clean-upgrade passed; failed image/lifecycle runs are not acceptance). Final
serial/repeated suite and Actions results are reported in the Draft PR.
The standard bookworm run initially failed before
tests with ENOSPC on Docker VFS. Only cache IDs/images created by this task were
removed; volumes were retained. Local execution uses the already-built runner
flattened into one layer plus read-only current tests and PG16 Alpine via a
temporary override, with the same SyntheticStack/marker/role/migration guards.
Neither this environment override nor trust roots/runtime secrets enter Git.

Actions retains all seven jobs. Its explicitly named database step runs
`make integration-ci`; the full dispatcher unconditionally schedules the complete
commercial suite on its own fresh standard PG16 bookworm stack, alongside all
prior suites. The existing scheduler guard verifies every SUITES entry executes
exactly once. `make commercial-regression` remains the standalone entry point.
No continue-on-error or new test processes against a shared database are added.
Final HEAD, PR URL and seven job links belong in the PR description, updated
after completion without changing the tested commit.

The first exact-HEAD Actions run at `5e6df43770405d37835de59395abc3a08c23a875`
passed all six other jobs. Its database job passed the explicit commercial run
(64 cases, 92.24 s including stack/build) and every prior PG/files suite, then
reached the 15-minute job deadline during the duplicate commercial run.
[Cancelled run](https://github.com/h1roshigoto/akbrizcase/actions/runs/42207952968).
The duplicate workflow invocation was removed: the full dispatcher's mandatory
commercial run is retained. No timeout, assertion or concurrency bound changed,
and no failed race was retried until success. Final CI is a separate run on the
updated HEAD; cancellation is not reported as successful acceptance.

## 9. Acceptance

PASS: new PG16 suite and local backend/guard checks above.
BLOCKED locally: full two-stack PG regression, ENOSPC (not a domain PASS).
Exact-HEAD seven-job CI and final repeat counts: see the maintained delivery
table in [Draft PR #57](https://github.com/h1roshigoto/akbrizcase/pull/57),
updated after the final commit's workflow completes without changing its HEAD.
NOT RUN locally: full Compose/Chromium, release, encrypted restore and operator
rehearsals; these remain mandatory Actions jobs, not inferred from the new tests.
BLOCKED: production/legal gates below. Collection/skips/xfails are never PASS.

## 10. Production/legal gates

BLOCKED: real PII/SMTP, production sink and durability/discovery, retention/legal
approval, ADR-038-A acceptance, service-policy/legacy/hold rules, operational log
TTL and production release/pilot gates MVP-037/038/041/042/043. Synthetic journal
evidence does not close these gates. Timing side channels remain separately
unproven; matching HTTP envelopes/headers do not establish constant-time behavior.
