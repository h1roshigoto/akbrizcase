# MVP-046 — сквозная browser E2E приёмка

Base: `465c1da68b3cede8bdd7434161f86fe9737473d5`. Branch:
`mvp-046-browser-e2e-acceptance`. Exact final HEAD and workflow URLs belong
in the Draft PR (a commit cannot contain its own SHA).

## Inventory before implementation

| Journey | Existing coverage | Missing link | New E2E | Required evidence |
| --- | --- | --- | --- | --- |
| A commercial → manager → worker | request-form, catalog-admin, notification-retry | Retry suite creates its own order by API; separate stacks | A/E in mvp046-e2e | React mixed order, real lost 201, SQLAdmin same receipt, PG snapshots/history, actual SMTP failure/retry, no business writes |
| B knowledge → file → withdrawal | live-chat, files_smoke, MVP-045 | Browser citation/download and subsequent lifecycle change in one environment | B | exact version, bytes/SHA256/headers, current/history/replay ACL after COMMIT |
| C diagnose → support → manager | live-support, live-chat | Feedback/adversarial input, commercial selection isolation and exact manager comment linked together | C/E | real UI, no automatic order, exact comment, consent/FK/history after chat deletion |
| D image/applicability | catalog-media, catalog-admin, MVP-045 | Images and internal evidence permission on the same ordered positions | D | SQLAdmin forms, list/detail/fallback, current keys, internal source absence, revoke changes total/status |
| E conflicts/network/expiry/limits | request-form, live-support, http-security | Preserved open input and genuine rate-limit recovery alongside A–D | E | real 409/410/429, fresh explicit review, exact retry credentials and committed counts |

The new runner must keep all modules in one fresh labelled PG16/files stack,
using the checked-out frontend/backend, verified localhost HTTPS, React,
SQLAdmin, real ClamAV/Pillow with explicitly synthetic signatures, and a closed
SMTP double. It must never orchestrate independent existing smoke entry points
as a substitute for these links. Existing standalone smokes remain mandatory.

## Admission boundaries

`LOCAL_SYNTHETIC_E2E: NOT RUN` (implementation in progress).
`TARGET_STAGING_E2E: BLOCKED`: no target server/domain/TLS, authorized full
profile, operator access or customer/expert approvals supplied. Reduced
MVP-041 release mode remains unchanged. Real PII, production SMTP, deployment,
MVP-047–050 and merge are outside this task. Synthetic search matches prove
no expert-corpus relevance. Synthetic diagnostic trees and review packets
prove no safety approval for real equipment.

Remaining gates: real content rights and expert reviews; legal/privacy policy,
retention/deletion decisions; signed CVD supply and monitoring; RF hosting,
SMTP and secret custody; independent production recovery/RPO/RTO; operator and
customer pilot authorization. No synthetic PASS closes those gates.

## Implementation and checkpoints

`make mvp046-browser-e2e` builds the current checkout, claims fresh labelled
volumes, migrates PostgreSQL 16, verifies the synthetic marker and seeds only
through the established synthetic/domain pipelines. Browser/SQLAdmin actions
and fixed domain transitions use one system. The closed SMTP double has no
published ports; worker ticks use the real lease/transport/backoff implementation.
Document delivery uses the same test-only real ClamAV composition as images;
the production signed-CVD gate is unchanged.

Fifteen mandatory checkpoint IDs: `A.desktop`, `A.mobile`, `B.desktop`,
`B.mobile`, `C.desktop`, `C.mobile`, `D.images`, `D.applicability`, `E.409`,
`E.410`, `E.429`, `E.network-before`, `E.network-after`, `keyboard.desktop`,
`keyboard.mobile`. Missing/duplicate checkpoints, missing receipts or unexpected
final writes fail the runner. `MVP046_EVIDENCE` contains only HEAD, case IDs,
counts, PG version, duration and the external gate. It is emitted only after
all mandatory browser and database assertions, not from test collection.

| Scenario | Intended order/consent/item/outbox/history delta | Evidence asserted |
| --- | --- | --- |
| A.desktop | 1 / 1 / 2 / 1 / 2 | lost real committed 201, exact manual 200 replay, SQLAdmin snapshots, status actor/version/history, worker failed→protected retry→actual SMTP sent |
| C.desktop | 1 / 1 / 0 / 1 / 1 | permitted model/scenario, forged/free input denied result, feedback, edited exact description, no POST on open, witnessed chat cleanup, FK NULL and manager comment |
| A.mobile | 1 / 1 / 2 / 1 / 2 | consent/price changes give actual 409, preserved fields, explicit review/reacceptance; first order snapshots unchanged |
| C.mobile | 1 / 1 / 0 / 1 / 1 | same support/feedback/cleanup/manager path at 390×844 |
| B / D | 0 / 0 / 0 / 0 / 0 | exact file download/checksum/headers; internal fact permission via SQLAdmin; revoke/history/replay; image upload/replace/failure/remove/fallback/archive; full business-row equality |
| E expiry / pre-send disconnect | 1 / 1 / 0 / 1 / 1 | own 410, preserved fields, explicit fresh site review/secrets, no chat header/FK; abort before transmission, exact manual retry, genuine 201 then original 200 |
| Total | **5 / 5 / 4 / 5 / 7** | one sent + four pending outbox, five distinct receipts; no duplicate writes |

Numbers above are executable expected postconditions, **not results of a run**
until the corresponding evidence is produced. Desktop is 1280×800; mobile is
390×844. Both commercial and support mandatory fields/confirmations/submission
use keyboard traversal. Focus, labels, Tab/Shift+Tab, Space/Enter, errors,
Back/Forward, double-click guard, receipt visibility and overflow are checked.
This is application acceptance, not a formal WCAG audit.

The message quota is filled using real authenticated requests behind Nginx;
forged X-Forwarded-For does not grant capacity. Recovery advances only the owned
synthetic session's existing quota window after the observed 429. Rates and
production configuration are unchanged; resubmission remains explicit.

Actual API boundary inherited from MVP-045: commerce has no model input and
returns applicability=null. Model-specific confirmed/unknown and total are
checked in catalog; this suite must not invent a model-aware commerce contract.

## Defect and validation ledger

Fixed: chat query text was cleared before the server answered. A real 429 would
lose the input. Chat now retains the draft on error, explicitly explains the
rate limit, keeps current context and performs no automatic retry. A component
regression proves retained text and exactly one call before explicit continuation.
Successful text submission still clears the accepted draft.

Local results so far (dirty implementation checkout):

| Command | Result |
| --- | --- |
| `make backend-test` | 1009 PASS, 32.50 s in the serial run |
| `make backend-lint`, `make backend-types` | PASS, 223 typed files |
| `npm --prefix frontend test` | 219 PASS, 13 files |
| `make frontend-typecheck frontend-build` | PASS |
| `uv run --locked python -m app.contracts.export_types --check` (backend) | PASS |
| `make acceptance-guards` | 50 PASS, including missing/duplicate journey and corrupt-count negatives |
| `make config`, `make local-tls` | PASS |
| `make build` with session CA mount | PASS before later source changes; not final acceptance |
| New E2E attempts | BLOCKED before browser: build CA omission, then Docker VFS ENOSPC; no complete browser PASS |

Initial overlapping backend/frontend/build checks produced one backend import
timeout and five frontend timeouts. Serial execution passed without changing
any assertion or deadline. The cloud-specific optional build CA is now passed
through a BuildKit secret mount. No TLS verification was disabled. Ordinary
builds do not require that optional cloud variable.

Docker started empty (zero images/containers/volumes). Only this task's build
cache was reclaimed after ENOSPC; no volumes or existing data were removed.
Full PG16 regression and prior browser runs require a runner with enough disk;
they remain mandatory in the unchanged seven CI jobs. A separate eighth job
runs the new controller twice on independent fresh stacks at the exact PR HEAD,
with a 20-minute outer budget; existing Compose timing/assertions are unchanged.
Final measured CI timing/results and source HEAD will be added to the Draft PR.

All input is synthetic, contacts use `.invalid`; credentials are transient.
Raw browser errors, SQL output, bodies, contacts, cookies, tokens, HAR, traces
and screenshots are not uploaded. Only safe stage locations and aggregates
are printed on failure. The existing final private log/audit scan includes
API/Nginx/PG/worker/SMTP and checks the observed authentication/contact material.
A failed or unexecuted scan is not evidence of absence of leaks.

Changed files: workflow, Makefile, browser media test composition, chat page and
controller, chat UI regression, shared HTTPS/SMTP harness helpers, new linked
browser/controller/domain fixture/evidence guards, and this report. No migration,
release-mode enablement or business-domain rewrite; the Nginx canonical response header fix is described below.

First exact-HEAD CI attempt (`03eb44ab0c16485a56d3b1acd1b4e760e7ebacea`,
run [42360128043](https://github.com/h1roshigoto/akbrizcase/actions/runs/42360128043)):
new suite **FAIL** at B.desktop, 101.53 s. Actual checkpoints passed:
keyboard.desktop (zero writes), lost committed response/replay (1/1/2/1/1),
A.desktop including worker/status (1/1/2/1/2), C.desktop including witnessed
cleanup/manager (2/2/2/2/3). Root cause: new browser assertion expected the quote
without the existing knowledge response's heading and numbered citation prefix.
The assertion now compares the exact established full text; no API behavior or
security predicate was changed. The failure is not counted as a full E2E PASS.
Also tightened outbox identity equality and moved aggregate PASS emission after
the final private log/audit scan. Next exact-HEAD CI must verify the whole suite.

Second attempt (`627887d0ad5417d2ebd46891be860f8e8721cb27`,
[run 42360390735](https://github.com/h1roshigoto/akbrizcase/actions/runs/42360390735)):
**FAIL**, B.desktop, 90.78 s; same four preceding checkpoints passed.
The remaining B failure is under investigation: granular citation/file/download
stage and source-line diagnostics are added without disclosing browser errors.
HTTP file headers/checksum are checked before the actual browser download to
separate delivery failure from browser event observation. Subsequent changes
also explicitly cover commercial Back/Forward, manager mobile viewport, matching
internal-search exclusion and deterministic SQLAdmin navigation completion.

Third attempt (`821690ae8fa3a23661951e4bfe26499c9fbf442e`,
[run 42360673607](https://github.com/h1roshigoto/akbrizcase/actions/runs/42360673607)):
**FAIL**, B.file-http.desktop, 78.72 s. This localized failure to the file
security-header assertion, before browser download. Configuration analysis found
that API middleware and Nginx both added X-Content-Type-Options, yielding duplicate
nosniff fields at the proxy. Nginx now hides the upstream copy and emits exactly
one canonical nosniff, including error responses. The same small fix applies to
HTTP, HTTPS, packaged default and reduced release Nginx configurations; no release
mode/rate/admission/scanner setting is enabled or relaxed. Existing HTTPS auth and
HTTP security smokes now assert a single effective header, in addition to the
strict file assertion in B. Next CI verifies actual corrected delivery.

Additional linked checks: real chat expected_version 409 followed by explicit
button choice; original commercial receipt replay after consent/price/image/
archive changes; unchanged business rows across each domain content mutation;
unchanged outbox identity across notification retry. These add no extra orders.

Fourth attempt (`b8236ca004281810b8e89999e01c9f08ad978ce8`,
[run 42361085189](https://github.com/h1roshigoto/akbrizcase/actions/runs/42361085189)):
**FAIL**, quota-recovery harness assertion, 105.84 s. Both A/C viewports,
commercial Back/Forward, keyboard, actual consent/price/staff/chat conflicts,
and both exact browser file downloads/security headers/checksums passed before
failure. Nginx header correction is thus exercised. Actual 429 and retained
query were observed. Root cause: psql `UPDATE … RETURNING` includes the `UPDATE 1`
command tag; comparing its stdout with just `30` was wrong. Recovery now uses a
CTE and SELECT asserting exactly one moved row with used=30. No rate threshold,
retry, HTTP success or business invariant is relaxed. Nginx `-t` also passed
locally for all four changed configurations in networkless read-only containers.

Fifth attempt (`76145e38216b9b2bad5e276c4111d98e6f7f6bea`,
[run 42361392429](https://github.com/h1roshigoto/akbrizcase/actions/runs/42361392429)):
**FAIL**, D.applicability visible filtered count, 115.51 s. E.429 including the
real quota, retained text and explicit continuation now passed. Next diagnostic
adds a committed prerequisite join and observes the actual browser model-filter
HTTP response before asserting its rendering, retaining the one-item requirement.

Sixth attempt (`da3d5fa203b7709af0d2e0c1c3068ac576f22686`,
[run 42361803015](https://github.com/h1roshigoto/akbrizcase/actions/runs/42361803015)):
**FAIL**, E.429 refresh control availability, 101.85 s; prior A/C checkpoints
passed. The unpaced quota burst mixed the session quota with the unchanged shared
Nginx budget and reused a window opened earlier in B. The quota case now owns a
fresh session and paces each real request at 650 ms, below 120/minute ingress,
within the original 60-second message window. A new direct PG assertion requires
used=30 with a live window, so an edge 429 cannot masquerade as message quota.
The UI refresh's actual HTTP status is asserted before further actions. This is
bounded request scheduling, not retry-until-success or a timeout/rate change.
