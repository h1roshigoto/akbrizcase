# MVP-045 — ACL, publication and diagnostic regression

Base: `b086981d48ea26a1537fa58ea520dd9be93e21a3` (merged MVP-044, PR #57).
`git fetch origin` confirmed this exact `origin/main`; the worktree was clean.
Branch: `mvp-045-acl-publication-regression`. The final HEAD and exact-HEAD
Actions job URLs are maintained in [Draft PR #58](https://github.com/h1roshigoto/akbrizcase/pull/58), avoiding a self-referential
commit. Synthetic fixtures only. No merge, deployment, production operations,
real personal data or MVP-046 work.

## Coverage inventory and requirements

The inventory below was made before adding the linked suite. Existing detailed
module tests remain in their original suites and execute once in integration-ci.
The new suite exercises shared dependencies across modules rather than importing
existing test functions under a second identity.

| §8 | Existing detailed evidence | New linked evidence |
| --- | --- | --- |
| 6 | `test_kb_reads`: ACL before ranking/limit/count, exact/file reads, staff expiry/disable/logout; `test_chat_reply_projection`: public cookie boundary | `test_acl_status_visibility_allowed_matrix_and_staff_cookie_is_not_chat_authority`: public/internal × published/draft/retired/revoked × assistant_allowed; count/page, exact source, files, new chat/history/replay. `test_staff_authority_loss_closes_internal_reads_but_public_chat_remains_public`: stale cookies after actual logout/disable/expiry |
| 7 | `test_catalog_applicability`: all missing predicates and DB CHECKs for publisher/time; SQLAdmin separate review/publication | `test_internal_evidence_confirms_fact_without_leaks_in_catalog_commerce_and_replay` for product/service; `test_internal_fact_requires_every_prerequisite_after_commit`: expert review alone, missing public permission, retired evidence, nonpublic item/model |
| 8 | `test_kb_lifecycle`, applicability invalidation, projector corruption/action rebuilding | `test_linked_revoke_propagates_to_all_public_surfaces`: same source → real applicability/tree publication → HTTP knowledge/diagnosis → version/document revoke or visibility change → all public surfaces and committed revision/audit/permission postconditions |
| 9 | `test_tree_lifecycle`, exact source and projector retired guards | `test_retired_exact_pin_then_revoke_and_new_publication_never_resurrects_permission`: real new document edition, permitted old file/source/pin, search exclusion, new-start refusal, unknown applicability, then version revoke |
| 10 | `test_chat_messages`, `test_diagnosis`, scenario/menu tests: state integrity, 30-edge boundary, stale versions and invalid input | Real TCP HTTP: forged next/node/actor, unknown/current-node/other-node/other-version options, labels/result-like/free text, unknown model, mode reset, new start, 30 edges across actual API process restart, request replay/body conflict, two simultaneous messages |
| 11 | `test_admin_images`, staff HTTP and applicability SQLAdmin tests; operator handover | `test_real_sqladmin_direct_forms_and_cli_share_authority_without_actor_spoof`: anonymous/manager/disabled manager/disabled admin/active admin, direct real routes, forged actor, CSRF/Origin, permitted order reads; document/tree lifecycle remain CLI-only |
| 12 | `test_kb_import`, `test_kb_lifecycle`, `test_diagnostic_trees`, `test_tree_lifecycle`, `test_operator_handover`: actual hidden-input CLI, wrong/missing credentials, review binding, immutable data, check/preview, file finalizers, audit/flush/commit rollback | Actual CLI tree revoke with the same protected dependency graph and DB audit; spoofed actor arguments; staff-disable race. `test_linked_revoke_fault_rolls_back_status_permission_revision_audit_and_projection`: actual PG failures after writes and at COMMIT |
| 13 | `test_files.py`, `test_files_acceptance.py`, `test_catalog_images.py`, `test_catalog_image_binding.py`, `test_admin_images.py`: MIME/extension/signature, UTF-8/PDF, disallowed formats, byte/pixel/dimension limits, corrupt data, traversal/symlink/substitution, missing/stale/unsigned CVD, scanner/parser/resource failures, replaced/archived keys | Same freshly scanned/reviewed/public source used by catalog/tree/chat and real HTTP file delivery; visibility change during actual ClamAV and revoked old file URL. No clean-returning scanner mock |

`get_source` is a real internal read service, not an invented HTTP endpoint.
Commerce HTTP has no model input and correctly returns applicability=null;
the catalog's explicit model projection proves confirmed with sources=[] for
internal evidence. Technical diagnosis recommendations use the model from the
authorized exact tree pin. Missing publication actor/time combinations rejected
by DB CHECKs stay in the existing predicate/constraint suite.

Public→internal changes source disclosure, not separately published fact
permission. Document/version revoke and retirement clear the latter. Returning
an existing allowed REPEATABLE READ snapshot during an overlapping revoke is
permitted; a fresh request after COMMIT must hide the source. Persisted JSONB is
retained and checked to still contain the original logical source, proving that
history/replay use current projection rather than deleting the evidence to pass.

## Concurrency and failures

All new races use independent UoWs/connections or a real HTTP/CLI process.
`wait_for_blocker` observes `pg_locks`/`pg_blocking_pids` against the exact holder
PID, within the existing two-second bound. Events around real scanner/router/SQL
work prove overlap for reads which intentionally do not take content locks.
No test retries until success, lock/statement timeout increases or sleeps as
proof of contention. Fresh read transactions check committed postconditions.

| Race | Evidence and invariant |
| --- | --- |
| Revoke vs applicability publish | Document holder, observed blocked independent publisher, real lifecycle invalidation below CLI staff serialization; no resurrected permission |
| Revoke vs tree publish | Same observed parent-row contention; tree remains draft, no partial publication |
| Visibility vs file read | Real clamscan finishes and pauses; visibility transaction commits before final authorization; HTTP 404 with no file bytes |
| Revoke vs history/replay | Pause after actual session SHARE read establishes the snapshot; revoke commits independently; coherent earlier response, then fresh handoff |
| New tree version vs running session | Hold real routed transition; publish replacement before message commit; existing pin remains original retired version; new session pins latest |
| Staff disable vs CLI | Disable holds shared staff authority lock; actual hidden-input CLI process visibly waits; after commit authentication denied, no success mutation/audit |
| Repermission vs evidence replacement | Observe publisher waiting on old parent; service replaces evidence under sorted locks; contender detects candidate change; new evidence has no inherited review/permission |
| Two TCP messages | Same expected_version: one routed transition, coherent response/state/envelope; same request gets replay, different request conflicts; changed replay body conflicts |

Scoped `pg_fault` listeners issue real PostgreSQL `SELECT 1/0` after document,
applicability, tree or audit writes and at the outer commit boundary. Complete
relevant status/permission/publication/revision/audit snapshots are compared
after rollback; public projection remains usable. A subsequent normal revoke
then closes catalog and history. The hooks never replace the domain services.

## Shipped DAG inventory

`content/trees/README.md` explicitly labels the entire corpus SYNTHETIC ONLY;
there are no expert-approved manufacturer scenarios.

| YAML | Complete start→terminal paths |
| --- | --- |
| `branching.yaml` | observe / yes → finish; observe / no → specialist |
| `linear-v1.yaml` | observe / yes → finish; observe / no → finish |
| `linear-v2.yaml` | observe / yes → finish; observe / no → finish |
| `handoff.yaml` | specialist (zero edges) |

Seven paths, four valid files, five explicitly rejected invalid files
(action/alias/cycle/duplicate/tag). The inventory guard refuses a missing corpus.
Each valid file passes the actual strict parser, is rebound only to synthetic
fixture IDs/model/key, imported and published with explicit SYNTHETIC review,
and traversed by the real HTTP diagnostic engine. DFS checks reachability,
acyclicity, options, terminals, sources, recommended quantities and expert-only
handoff. Each path is also checked after source loss and recommendation loss;
history/replay revalidate dependencies. Placeholder UUIDs never become real
publication authority.

**BLOCKED external gate:** expert review of real technical content and the
approved diagnostic minimum. Structural/engine PASS is not expert acceptance.

## Execution and delivery

Entry points: `make acl-publication-regression` runs its own fresh labelled PG16
stack; `make integration-ci` includes it exactly once, with the existing
two-stack scheduler, inventory and real file acceptance. `core` excludes this
file to avoid duplicate execution. CI still checks out the exact PR HEAD and
retains all seven jobs and all safety/volume guards. No migration is needed.

Test implementation SHA: `ccc1a63eaaadf30707feaa652f9fd2f63b846243`.
Subsequent changes are CI budget, evidence comparison/guards and this report;
the 65 PostgreSQL test cases and application code are unchanged.

| Executed command/check | Actual result |
| --- | --- |
| `make backend-test` | **1009 PASS**, no failures; final local repeat 35.78 s |
| `make backend-lint`, `make backend-types` | Ruff/format and strict Mypy **PASS** |
| `npm --prefix frontend test` | **218 PASS** |
| `make frontend-typecheck`, `make frontend-build` | **PASS** |
| `make acceptance-guards` | **47 PASS**, including two new inventory comparison negatives |
| `make acl-publication-regression`, fresh project `akbrizcase-dbtest-0018c5071cc2` | **65 PASS / 0 FAIL / 0 SKIP**, pytest 357.72 s, guarded runner 386.18 s |
| Same command, independent fresh project `akbrizcase-dbtest-27b5c8818f31` | **65 PASS / 0 FAIL / 0 SKIP**, pytest 332.68 s, guarded runner 370.43 s |
| `make integration-ci`, local VFS | **BLOCKED**: clean-upgrade 1 PASS, full run deliberately interrupted after two stacks left <600 MiB, before starting the larger file stack; incomplete groups are not PASS |
| `make integration-ci`, standard exact-HEAD Actions | Entire command **PASS** on the implementation SHA above; new suite 65 outcomes, real files 119 outcomes; overall job **CANCELLED** at its 15-minute budget before artifact upload |

Both local new-suite runs used the same committed test implementation on
independent fresh PG16 stacks, with no retry-until-success or skipped race.
Their safe inventory files each contain exactly 65 distinct PASSED identities.

[Implementation Actions run 42336573653](https://github.com/h1roshigoto/akbrizcase/actions/runs/42336573653)
finished all six other jobs successfully. The PostgreSQL command completed
successfully at 04:16:49 UTC after starting at 04:01:59; the job started at
04:01:49 and reached its outer 15-minute deadline before inventory upload.
The full dispatcher recorded outcomes: clean upgrade 1, core 955, images 58,
lifecycle 43, applicability 75, trees 136, chat 174, HTTP security 11, commercial
64, new ACL suite 65 and real files 119. Core's two pre-existing skips are the
clean-upgrade and backup tests, each actually executed in its dedicated stack/job.
There were no skipped new ACL cases. The cancelled job is not a seven-job PASS.
The earlier run 42336306818 was superseded by the CLI readiness fix.

The PostgreSQL job budget is now 20 minutes for the measured added workload
and inventory upload. All seven jobs, assertions, two-stack ceiling, database,
scanner/parser and race deadlines are preserved. Final HEAD/run/job URLs and
the seven conclusions are maintained in the Draft PR delivery table after
the final commit's workflow completes, without changing the tested HEAD again.

Local infrastructure: standard integration runner built successfully; pulling
standard PG16 bookworm then failed with ENOSPC on the managed 8 GiB Docker VFS
filesystem. Local-only `/tmp` overrides use exported/imported single-layer
runner and PostgreSQL 16.13 Alpine, read-only current app/tests, and the same
SyntheticStack markers, runtime/migrator roles, migrations, limits and AV engine.
Only verified task-created images/build cache were removed; all test volumes
were preserved. Session CA/build overrides and secrets are not committed.
An attempted file-test invocation through the DB runner also exhausted its
32 MiB tmpfs; it is not file acceptance evidence. The existing file acceptance
runner's volume/tmpfs/network/resource contract remains authoritative.
The local full-run interruption used labelled recovery and an exact
project/service/image/oneoff ownership check before stopping its remaining
one-off test container; a second container had already exited. No volumes were
deleted. P2 infrastructure limitation: ordinary Compose down can leave an
active one-off runner after dispatcher termination; manual owner-verified
container stop was needed locally. This is not a content/auth bypass.

## Defects and remaining gates

No domain security defect was found in the completed scenarios. One actual
evidence defect was found: `compare_ci_tests.inventory` used a historical fixed
list and silently ignored newer PG artifacts, including chat/HTTP/commercial
and the new ACL suite. It now includes every provided DB group and requires
every group announced by CI; missing announced artifacts cannot fall back to a
partial console result. Two positive/negative guard cases also cover a future
group, omitted ACL artifact and absence of all announced artifacts. Older
inventories remain comparable; raw runtime logs are not exported.

Development failures are not counted as acceptance: initial fixture edit used
reconciliation rather than `edit_section`; the TCP harness needed the real
runtime IP-security initialization; wire resolution is `results` rather than
the internal node type `result`. Those test-preparation mistakes were corrected
without changing domain guards or weakening assertions.
The first staff-disable CLI race also exposed a harness synchronization error:
process/import startup consumed the two-second lock-observation window. The
existing PTY helper now has an optional password-prompt callback. The contender
is ready for authentication before disable takes its lock; only then are the
password and lock probe released. The same exact blocker PID and original
two-second observation/three-second DB lock bounds remain mandatory. No retry
or timeout inflation substitutes for observed overlap.

Open external gates: actual content rights/expert approvals, production signed
CVD delivery/update monitoring, legal/privacy/operations/pilot acceptance from
prior MVPs. No real-data or production readiness is inferred from these tests.
