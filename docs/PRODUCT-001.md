# PRODUCT-001 — Industrial Service Platform kickoff

Inspection date: 2026-11-13. Integration baseline: `a7943888409a798d389bba3f1d382cc2cbc3a549`.
Remote `refs/heads/main` was fetched and independently read with `git ls-remote`;
it matched that baseline. Development and stable checkouts were clean initially.
Implementation branch: `codex/design-001-foundation`. This document proposes
future product work; only DESIGN-001 is implemented by this branch.

## Environment and isolation

The chat's Windows workspace was empty and resolved to an unrelated ancestor Git
repository with no HEAD. No files were staged there. Work uses the requested
`/home/hiros/src/akbrizcase-dev`; `/home/hiros/src/akbrizcase-demo` is read-only
for this task. Stable ingress is `https://localhost:45767` and stable deployment
root `/home/hiros/.akbriz-front-demo` is never supplied to a mutation command.

Development is a separate `full-synthetic-staging` environment prepared through
`scripts/release_rehearsal.py --prepare-only`, checked and applied through
`scripts/release.py`. Private root: `/tmp/akbriz-product-001-dev`, outside Git,
mode 0700. Development URL: `https://localhost:35067`.
Namespace: `akbriz-staging-55456c8ca283`; database and file volumes use that
namespace, with their own networks, six generated secrets and localhost TLS.
The logical database name `akbrizcase` is fixed by the approved controller; its
PostgreSQL instance, storage and credentials are entirely separate from MVP.
No DB/worker port is published. SMTP stays in the isolated synthetic sink.

Initial controller check/apply passed TLS, migration, authority/marker,
readiness, modes, file and topology boundaries. Existing
`release_full_rehearsal.seed` generated only synthetic catalog, consent, staff,
knowledge and diagnostic fixtures in the new owned DB. Original commercial data
was not imported. ClamAV uses the explicit synthetic-signature test composition;
this is not evidence for signed-CVD production capacity. Private runtime material
and staff credentials are not Git artifacts. Do not enter real personal data.
The development CA is `ca.crt` in the private root. Trust that CA explicitly in
the browser used for local review; never disable certificate verification.
The root is under `/tmp`, so continuity across host cleanup/reboot is not promised.

A later Docker inspection found all five original stable containers retained but
stopped, with FinishedAt 2026-11-13 06:07:22–26 UTC (09:07:22–26 Istanbul).
They were running at the initial inspection. Stable checkout remains clean at
the baseline and its two volumes are retained. No task command targeted that
namespace for stop/start/recreate, and available Docker history did not establish
the cause. Stable availability therefore cannot be reported as preserved. The
task does not restart a possibly intentionally stopped deployment. Read-only
inspection verifies disjoint volume, network and bind-source sets even while
stable containers are stopped: [isolation evidence](design-001/isolation.json).

The host has no project-compatible native Python/Node toolchain. Node 24.19.0
and npm 11.9.0 were installed in `/tmp/akbriz-product-toolchain`; dependency
versions and lockfiles were preserved. Python checks use the project's Python
3.12.14 integration image. Pinned Playwright 1.58.2 does not recognize host
Ubuntu 26.04 and host dependency installation requires unavailable sudo. Browser
acceptance therefore runs in its official Noble image with a private NSS CA
store and `ignoreHTTPSErrors: false`. See DESIGN-001 for actual results.

## Actual architecture and findings

The existing modular monolith remains appropriate. React 19/TypeScript/Vite and
FastAPI/SQLAlchemy/PG16/Alembic/SQLAdmin are retained. `ARCHITECTURE.md` still
contains the historical wording “предстоит реализовать”, while the code implements
many of those sections. The root README is one byte; frontend README also
contains historical “до MVP-024/025” statements superseded by current code.
Treat schemas, domain services, tests and current source as implemented evidence.

| Finding | Source evidence | Product implication |
| --- | --- | --- |
| Hash navigation, page orchestration, selection, submission and chat ownership share one large component | `frontend/src/pages/CatalogPage.tsx`, `App.tsx` | Incrementally separate route shell from workflow owners; preserve persistent submission/controller instances. Do not introduce a router rewrite in DESIGN-001. |
| Catalog/models load even when the home/docs view is open | `CatalogPage.tsx`: unconditional `useResource(loadModels/loadItems)` | Measure unnecessary requests; route-aware acquisition must preserve stale-response and ownership behavior. |
| No central client data cache; abort/stale-result handling is already explicit | `state/useResource.ts`, `api/httpCatalog.ts` | Retain fail-closed behavior; measure before adding a query library. |
| Selection rereads each ID separately, up to 100 items; request review has its own reads | `components/SelectedItems.tsx`, `pages/RequestPage.tsx`, `state/selection.ts` | Bounded concurrency/caching is a candidate; add a batch API only after traces prove need and retain current ACL/revision checks. |
| Two large global stylesheets; tokens mixed with imported styles, duplicate button families and layout rules | `styles.css`, `design/assets/design.css` | DESIGN-001 extracts theme tokens and the existing shared control rules; staged adoption avoids portal-wide redesign. |
| Dark field boundaries and light/dark focus need explicit component-level evidence | original `design.css` `--field-line`; `styles.css` search/select rules | Pilot uses stronger semantic border/focus tokens; full WCAG 2.2 AA remains a broader audit. |
| Catalog has `StatusPanel`; documents and request/chat own separate feedback markup | `components/StatusPanel.tsx`, `pages/DocumentsPage.tsx`, `pages/RequestPage.tsx`, `chat/ChatPage.tsx` | Keep workflow-specific wording/authority; adopt common visual primitives gradually. |
| Search matches name/SKU and model filter requires published confirmed evidence; arbitrary attribute search is absent | `backend/app/modules/catalog/repository.py`, `schemas/catalog.py` | Add typed attribute facets only after an approved equipment taxonomy; do not promise current “search all characteristics”. |
| Public assets and WebGL are statically imported into the page graph | `CatalogPage.tsx`, `pages/HomePage.tsx`, `design/ModelViewer.tsx` | Current build has one JS entry; measure route parse/load costs and consider lazy boundaries without losing form state. |
| Brand logos, Golos fonts, equipment poster/GLB and footer photo already exist locally | `frontend/src/design/assets`, `docs/FRONT-INTEGRATION-001.md` | Reuse original identity; a rendered GLB poster is not a verified product photograph. Publish actual photos only through reviewed catalog media. |
| Manager orders and admin forms are server-rendered protected workspaces | `backend/app/admin/orders.py`, `templates/orders`, `admin/catalog.py`, `admin/content.py`, `admin/staff.py`, `admin/stats.py` | Improve existing surfaces incrementally; a new React staff client is not required to achieve a professional workflow. |

Keyboard support already includes native buttons, search Enter, modal drawer
focus/Escape/restore, menu Escape, focus styles and reduced motion. Full navigation
focus after hash transitions, sticky-header occlusion, 200–400% zoom, target
sizes outside the pilot, screen-reader announcements, validation associations,
forced colors and every theme contrast pair still require manual/automated
coverage. Current browser evidence is scoped, not a WCAG certification.

## Security and transaction boundaries to retain

`backend/app/core/db.py:Database.unit_of_work` commits once, rolls back on any
BaseException and keeps release marker validation outside the pristine domain
transaction. `modules/orders/service.py` and related services retain snapshots,
consent, replay ownership and transactional outbox. `orders/status_service.py`
locks rows and checks expected_version with audited transitions.
`catalog/write_service.py` checks admin authority, uniqueness and revisions.
`catalog/applicability*.py`, KB lifecycle/tree services and file routes recheck
publication/evidence/ACL at reads and mutations. Public knowledge fetches omit
staff credentials (`frontend/src/api/httpKnowledge.ts`).

Do not duplicate backend business logic in React. Preserve the one client
`frontend/src/chat/controller.ts:ChatController` across wizard/chat views and
the server chat authority (`backend/app/modules/chat/messages.py` and
`mode_router.py:ModeRouter`). The literal class `ChatController` exists on the
client; no server class with that name is present in this checkout. Preserve
these existing server entrypoints rather than inventing another engine. Keep
RBAC, CSRF/Origin, TLS/secure cookies, trusted proxy/origins, rate quotas,
idempotency, session ownership, expected revisions, AV/quarantine, audit/privacy,
backup/restore and release authority. No API DTO, migration, security limit,
release mode or publication rule changes are needed for DESIGN-001.

## Product areas: existing capability, gaps and acceptance

“Implemented” below means source-backed; verification scope is listed separately
in DESIGN-001. Future acceptance is a proposed release gate, not a present PASS.

### A. Public website — P1

Exists: `pages/HomePage.tsx`, `TechnicalPage.tsx`, `components/SiteHeader.tsx`,
`design/ModelViewer.tsx`; home, theme, navigation, equipment visual, synthetic
trainer and support CTA. Local original assets already work through Vite.
Problems: home is orchestrated by catalog shell, unconditional catalog fetches,
static view imports, synthetic footer, no approved company/content publication
workflow for these static sections. No dedicated service detail/corporate CMS.
Reuse: public equipment-model and catalog/document APIs.
Backend: none for editorial layout; only a separately scoped, reviewed content
service if nontechnical editors actually need dynamic content.
Dependencies: approved company copy, claims/contacts, rights and photo inventory.
Risks: implying unverified manufacturer/distributor authority or technical facts.
Acceptance: approved content inventory with provenance, real CTA destinations,
keyboard focus on navigation, 390/768/1440 layouts and measured asset budget.

### B. Catalog and details — P0 data / P1 UX

Exists: `CatalogPage.tsx`, `CatalogCard.tsx`, `ItemDetail.tsx`, `ItemFacts.tsx`,
`ItemImage.tsx`; search, pagination, products/services, model context,
confirmed/unknown applicability and safe missing-image behavior.
Reuse: GET `/api/v1/equipment-models`, `/catalog/items`, `/catalog/items/{id}`,
item image URLs; existing protected catalog, applicability and image commands.
Problems: name/SKU-only search, generic attribute display, many detail requests,
style duplication; taxonomy/facets and import-review workspace absent.
Backend: provenance/import deduplication and typed facets only as separate
contract changes with approved source schemas; current catalog APIs are reusable.
Dependencies: data onboarding, evidence documents, expert approval and media AV.
Risks: unknown shown as incompatible, unverified prices or auto-published links.
Acceptance: product/service and missing-price regressions pass; source/review
records cover every published item; revoked evidence immediately loses confirmed;
search facets never expose unpublished/internal data.

### C. Cart and commercial request — P1

Exists: `SelectedItems.tsx`, `SelectionDrawer.tsx`, `RequestPage.tsx`,
`state/money.ts`, `selection.ts`, `submission.ts`; integer money, revision
review, unknown subtotal, explicit consent, owned idempotent retry and receipt.
Reuse: catalog reads, GET consent/privacy policy, POST `/api/v1/orders`.
Problems: per-ID revalidation, large form component, repeated control markup;
selection allows 100 while submission explicitly caps 50 (intentional documented
contract, not silent truncation). No customer organization workspace or saved quotes.
Backend: none for next UX slice; accounts/quote lifecycle require separately
approved identity and privacy design.
Dependencies: common controls, legal approval, commercial workflow acceptance.
Risks: duplicate orders after uncertain response, lost revisions/consent semantics.
Acceptance: lost response replays same payload/key/token, 409 requires review,
unknown price never becomes zero, no contacts in browser storage, modal keyboard
and server-side validation remain covered.

### D. Diagnostic workspace — P0 content/performance / P1 UX

Exists: `chat/ChatPage.tsx`, `controller.ts`, reply/source components and
`backend/app/modules/chat`; shared chat/wizard owner, expected_version/request_id,
server-owned published graph and support handoff.
Reuse: `/api/v1/chat/scenarios`, `/sessions`, owned session reads/messages and
existing reply actions/source DTOs.
Problems: layout and feedback differ from catalog; static training visual must
remain clearly separate from diagnostic instructions. No approved new equipment
trees can be inferred from source assets. Known button latency gate remains open.
Backend: no diagnostic engine rewrite; new trees use existing expert publication.
Dependencies: approved model/scenario/source coverage, performance investigation.
Risks: stale actions, revoked sources, duplicated controller or leaked session tokens.
Acceptance: one controller survives presentation switch, stale version conflicts
refresh safely, revocation/replay/expiry/ownership tests pass; no frontend next-node
logic or invented instructions; original latency gate passes independently.

### E. Knowledge center — P1

Exists: `pages/DocumentsPage.tsx`, `api/httpKnowledge.ts`, `chat/SourceLink.tsx`,
KB import/lifecycle/files/search services; public list, kind filter, FTS excerpts,
validated version links, ACL and revoked-version handling.
Reuse: GET `/api/v1/documents`, `/knowledge/search`, exact version file route.
Problems: separate feedback markup; no approved topic hierarchy, browse-by-model
or richer source-revision explanation. Search mode disables kind filter explicitly.
Backend: none for visual consistency; model/topic filters need approved metadata
and indexed query contracts if justified.
Dependencies: original document bytes, publication rights, AV and expert review.
Risks: staff cookie exposing internal content, old source interpreted as current.
Acceptance: anonymous list/search omit internal/revoked material, links point to
exact validated versions, old version labels are clear, empty/error/retry keyboard
flows pass; source authenticity is separately reviewed.

### F. Customer support — P1 / P2 account workflow

Exists: `RequestPage.tsx` support context and chat handoff, `Submission`, order
services; independent support or owned diagnostic summary, explicit confirmation.
Reuse: existing support order contract and owned chat support-draft actions.
Problems: long shared request component, no customer authenticated ticket timeline,
SLA/assignment attachments workflow or durable customer account.
Backend: none for existing support form refinement; account/ticket features require
identity, ownership, retention, ACL and upload contracts before UI implementation.
Dependencies: legal contact use, manager queue, approved support process.
Risks: cross-customer disclosure, unconfirmed machine-generated summaries.
Acceptance: edited summary requires confirmation; invalid/expired ownership fails
closed; manager sees committed description after chat cleanup; no real PII in dev.

### G. Manager workspace — P1 / P2 operations

Exists: protected SQLAdmin order list/detail, status history, optimistic version,
notification retry and summary statistics; `admin/orders.py`, `admin/stats.py`,
`modules/orders/read_service.py`, `status_service.py`, `templates/orders`.
Reuse: GET `/api/v1/admin/orders[/{id}]`, PATCH `/{id}/status`, existing protected
notification retry. Keep DB-backed authorization and server templates initially.
Problems: generic staff presentation; no assignee/team queue, SLA, saved filters,
quote versioning or unified communication timeline in inspected order model.
Backend: assignment/SLA/quote persistence only after workflow approval and
concurrency/audit design; no backend change for density/readability improvements.
Dependencies: real manager journey observation using synthetic cases and roles.
Risks: lost updates, unauthorized PII visibility, accidentally storing sensitive notes.
Acceptance: role denied matrix, concurrent expected_version conflict, 50-row queue
keyboard usability and exact totals/history; no weakening of staff session controls.

### H. Administration — P0 data / P1 ergonomics / P2 governance

Exists: catalog/model draft forms, review/publish applicability, image upload,
KB/document/tree publication and staff lifecycle with RBAC/audit; `admin/catalog.py`,
`catalog_applicability.py`, `catalog_images.py`, `content.py`, `staff.py`.
Reuse: existing admin adapters and domain commands; direct CRUD must not bypass
publication validators. Statistics already exist, so do not build a duplicate MVP.
Problems: separate public/staff design systems; source-onboarding/deduplication,
review queue and provenance ledger absent as integrated workflows.
Backend: auditable import batches/source mappings and explicit review state,
added separately with linear Alembic migrations and protected domain services.
Dependencies: owner approval rules, source archive inventory, scan pipeline.
Risks: duplicates, stale approval after edit, evidence/rights disclosure.
Acceptance: invalid draft cannot publish, edits reset approvals where required,
failed upload preserves old image, revoked evidence is effective immediately,
import replay is idempotent and staff/audit/ACL regressions remain green.

## Prioritized roadmap and dependencies

| Priority | Deliverable | Depends on / completion gate |
| --- | --- | --- |
| P0 | Separate synthetic development and DESIGN-001 bounded foundation | Controller readiness/isolation, baseline/after screenshots, positive/negative UI tests; this branch. |
| P0 | Performance investigation PERF-002 | Reproduce original workload/thresholds; button p95 540.29 ms vs 300 ms remains an open reported gate, API peak 512 MiB at hard cap. No production admission. |
| P0 | Catalog/source onboarding design and content approval | Owner archive inventory, price/applicability/rights verification, existing AV/expert/publication services. |
| P0 | Release/legal/operational evidence | Existing MVP-050 gates: signed CVD, approved legal/company content, operator authority, privacy, backup/clean restore and real host evidence. |
| P1 | DESIGN-002 catalog controls and detail hierarchy | DESIGN-001 + catalog tests; preserve APIs and statuses; review desktop/tablet/mobile keyboard/zoom. |
| P1 | Public content and knowledge navigation | Approved copy/photos/documents; scoped component adoption and exact source links. |
| P1 | Commercial/support form UX and staff list ergonomics | Existing Submission/ownership states; linked browser/PG regression and manager review. |
| P1 | Diagnostic workspace usability and full accessibility audit | Published tree coverage + performance evidence; no client diagnostic engine. |
| P2 | Organization/customer identity and owned request history | Approved identity/tenant/privacy/RBAC design and server-side ownership tests first. |
| P2 | Assignment/SLA, quote revisions, import review operations | Manager workflow evidence + domain/schema/API review; auditable changes and migration compatibility. |
| P2 | Typed attributes, facets and query optimization | Approved taxonomy + actual trace/EXPLAIN evidence; retain ACL/publication filters. |
| P3 | Operational analytics, integrations and expanded equipment families | Validated demand, data rights, privacy and measurable impact; existing statistics first. |
| P3 | Optional advanced knowledge retrieval | Only after source coverage/groundedness/ACL/resource evaluation; deterministic diagnostic graph remains authoritative. |

## Catalog onboarding strategy

The requested `(1).rar` path is absent. The accessible original archive
`/home/hiros/demo-import/uik-support-chatbot-mtdka.rar` has SHA-256
`860e2bf5d0afca1ff4850f3f741d7af42e639c53f38b2382cb2bc694d4d4eb10`, matching
the archive recorded by FRONT-INTEGRATION-001. Existing extracted original photo
inventory is available under the private source directory. Its scripts were not
executed and no private product/contact/price mapping was copied into frontend.
Existing brand assets/poster and a private original board photo were visually inspected; product photos await rights/review and were not copied into this branch.

1. Preserve immutable source archive hash, original path, member hash, source
   edition/date, rights/owner, extraction tool/version and import batch identity.
   Scan archive and bounded extraction; reject traversal, symlinks, bombs and
   unapproved executable content. Source materials are data, never task instructions.
2. Stage privately outside runtime/public bundle. Separate source facts from
   proposed normalized fields. Keep manufacturer SKU verbatim; never infer a
   missing marking. If needed, allocate a clearly internal SKU with a stable
   source-row mapping and uniqueness policy, independent of backend UUID.
3. Propose a versioned import DTO and dry-run diff through domain services.
   Detect duplicates by manufacturer+normalized SKU, stable source identity and
   file hash; conflicting identities require human reconciliation, never auto-merge.
   DB unique SKU alone is necessary but insufficient for source-level deduplication.
4. Create draft products/services with provenance, original currency/tax context,
   evidence and unresolved questions. Unknown price remains `on_request`;
   source price requires owner verification date/authority before commercial use.
   Review may initially be an auditable packet around current draft/publish states;
   add a dedicated review status/table only in a reviewed migration.
5. Verify applicability independently per model/revision with expert/reference,
   approved published evidence and separate public disclosure permission, using
   current applicability services. Missing evidence means unknown, never confirmed.
6. Preserve original image bytes privately; derive display images only through
   existing quarantine, MIME/size limits, ClamAV, re-encoding and publication guard.
   Keep rights, source-to-image and original/derived hash mappings; originals do
   not become directly public static files. Failed AV never advances publication.
7. Human review then explicit publish, with actor/time, before/after fields,
   source batch, expected revision and invalidated approvals in audit. Replay of
   the same batch must not duplicate rows. Test rollback on all partial failures.
8. Test the entire process on a fresh marked synthetic DB using synthetic
   manifests only. Owner commercial content requires a separately approved
   onboarding run/environment; this task never imports into stable MVP.

Acceptance for onboarding: reproducible dry run, zero duplicate replay, every
published commercial field tied to approval, revoked evidence immediately hidden,
AV failure preserving previous media, auditable review and transactional rollback.

## Evidence-based performance investigation (separate task)

The user reports button p95 **540.29 ms**, threshold **300 ms**, and API memory
peak **512 MiB** at the hard limit. Historical PERF-001 documents multiple other
runs/values; do not replace the reported open gate with a convenient older PASS.
These are baseline reports, not measurements from DESIGN-001.

Use an isolated new namespace and exact SHA/image/source/dataset identities,
trusted TLS and the unchanged pilot200-v1 workload (200 clients, 30 sources,
peak concurrency 30). The active walkthrough DB must not become a load fixture.
Reuse `scripts/mvp048_smoke.py`, `perf001_timing_result.py`, `perf001_probe.py`
and `backend/app/core/timing*.py`. Record actual all-attempt p95, status/coverage,
pool queue/acquisition/hold, SQL count/wall, worker scheduling/CPU, commit and fresh
projection; inspect correlated EXPLAIN plans and lock waits without payload logging.

Measure cgroup memory.current/peak/events, OOM counters, RSS per Uvicorn process,
Argon2 + actual AV overlap and signed-signature footprint in the original 512 MiB
resource envelope. Synthetic-signature success cannot certify signed CVD capacity.
Check CPU throttling, connection churn and PG budget (3×8 API connections plus
bounded other classes and release guard pools). Existing capacity experiments
already show increasing pools alone can worsen latency.

Compare one evidence-backed change at a time on the same host/workload with
repeated runs, warm/cold observations and functional/security regression. Keep
thresholds/resource limits fixed; report uncertainty and tail distributions.
Potential frontend route splitting and bounded detail loading require separate
browser waterfall/interaction/heap measurements. No threshold changes, retries
that hide failed attempts, new unapproved target load or production-ready claim.

## Next implementation tasks

1. PERF-002: reproduce the reported latency/memory gates with correlated evidence.
2. DATA-001: source manifest/dry-run/review DTO and duplicate policy with owner.
3. DESIGN-002: catalog/detail control adoption and screen-reader/zoom acceptance.
4. WORKFLOW-001: observe synthetic manager/support cases and improve queue/forms.
5. KNOWLEDGE-001: approved document/model navigation and consistent source states.

No automatic merge or stable-MVP deployment is authorized or performed.
