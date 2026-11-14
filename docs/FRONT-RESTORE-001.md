# FRONT-RESTORE-001 — local restoration continuation

PR: https://github.com/h1roshigoto/akbrizcase/pull/73. Branch: `codex/front-restore-001`.
Baseline remains `a7943888409a798d389bba3f1d382cc2cbc3a549` (latest origin/main at inspection).
The original cloud-only archive blocker is resolved. This is an **incomplete restoration**, not a 1:1 acceptance claim.

## Verified original and stable

On the owner’s WSL host, `/home/hiros/demo-import/uik-support-chatbot-mtdka.rar`
hashes to `860e2bf5d0afca1ff4850f3f741d7af42e639c53f38b2382cb2bc694d4d4eb10`.
The `(1).rar` alternative is absent. 7-Zip reports 345 entries: 291 files and 54 directories.
The six HTML pages, builder templates, JS/CSS, GLB, fonts and original E2E are available.
The nested v0.2 archive contains 42 entries and no PDF originals.
Original reference runs only on loopback at `http://localhost:45801` during review.

Stable remains the accepted main release on `https://localhost:45767`.
After Docker Desktop restarted, its existing stopped containers were resumed unchanged.
HTTPS was verified using its saved CA. The actual public catalog has **16 positions,
10 image URLs, two published models**. The owner-confirmed selective transfer into
review preserved all catalog UUIDs, fields, revisions and statuses. HTTPS checks
read every detail and all ten images through the real read/AV pipeline and verified
byte-identical image responses and identical public details; stable stayed unchanged. Synthetic privacy publication is enabled;
one public teaching document exists. No stable source, accepted image, database
record, price, publication status or volume was changed by this continuation.

## Original presentation adapters

- Original CSS is byte-identical to `mtdka-site/assets/style.css`. Existing React
  redesign overrides are followed by the original stylesheet plus narrow API adapters.
- Header has the original six links; home sections and catalog framing use original
  markup. Original HTML URL aliases resolve to the same React application.
- Catalog uses the original three-column cards, live search, model choices,
  add/quantity controls, detail panel and modal cart; it loads only `httpCatalog`.
- Request uses the original two-column form/summary. Existing Submission, revision
  review, exact money, consent/policy reads, explicit confirmation and replay remain.
  Delivery calculator retains the original honest “manager confirms” behavior.
- Assistant uses the original two tabs, quick choices and indicator panel. A single
  ChatController still owns session/token/version/retry/ACL behavior. Indicator
  questions go to server knowledge search, never to archive JS answers. Wizard
  choices load published models/scenarios; source and feedback buttons remain server-owned.
- 3D retains the original GLB and renderer, four original hotspot positions, popup
  placement/mobile sheets and original dimension toggle. Hotspot text is read from
  public knowledge APIs. Unsupported numerical dimensions are not asserted.
- Existing ItemImage exceptions remain unchanged: exact left-post SKU → «На заказ»;
  usable service photo first, neutral service icon otherwise.

No `data.js`, hardcoded archive assortment, local technical answer engine, raw KB,
real personal contacts, private material or external CDN is bundled.

## PR #72 compatibility

No merge/cherry-pick of #71 or #72. The request/policy recovery copy from #72 is
reused. Its stable synthetic privacy publication is verified as runtime state,
not reproduced through a new seed. Its three-mode assistant UI is not imported.
Existing commercial/support request mechanisms already present in main remain the
business owners. The same unchanged existing synthetic privacy packet was published in review through authenticated migrator authorization, with separate review staff. Two actual review orders (commercial and owned chat support) passed 201 creation, byte-identical 200 replay, protected manager reads, original selection snapshots and sent notifications. The closed SMTP sink received exactly two distinct order notifications. Only artificial contacts were used.

## Outstanding differences and publication gate

The archive’s original repair answers, source quotes, model-specific diagnostic
tables, original trainer boot/calibration/fault scenarios and technical parameter
table have **not** been published or claimed restored. Architecture §§3.1–3.3,
§10.2 and MVP-027/028 require actual source bytes, expert review and authenticated
publication. The current server has only a synthetic teaching corpus. Possession
of a ZIP/JS file is not an expert review record. No approval names or evidence are
fabricated. Owner input was requested for the approved source/review packet.

Consequently the trainer remains the pre-existing synthetic simulation. Hotspot
contents and dimensions differ until the corresponding material is published.
Home contact area uses synthetic role labels and the existing protected support
form rather than real personal contacts or the original fake-success quick form.
Request retains the backend-supported minimum contact fields and mandatory
privacy/review controls; unsupported institution type/street address are not
silently collected. Original static numeric part hashes are not backend UUIDs.
Published document catalog may differ from the archive. These are material gaps:
**ORIGINAL_DESIGN_MATCH, ORIGINAL_PAGES_RESTORED and ORIGINAL_FUNCTIONS_RESTORED
must not be marked YES.**

## Verification so far

- Before edits: 235 frontend tests passed.
- Current frontend: 248 tests passed; TypeScript and production build passed.
- Unchanged original reference: **106/106 original HTTP E2E passed**, using the
  repository’s pinned Playwright package. This certifies the reference, not candidate parity.
- Corrected read-only whole-repository backend run: 1166 tests, Ruff and mypy.
- Commit `6991ac7508c3cd5edfcb1bd25d69702bd69a4c96`: GitHub backend,
  frontend, PG16 integration, encrypted restore, load/recovery, operator handover,
  linked browser and full staging release rehearsal all passed. Compose reached
  a stale support-navigation expectation; that interaction is corrected and its
  full HTTPS journey passed locally. Final-head CI is recorded in the PR update.
- Local HTTPS support, privacy retirement/config loss, request persistence and
  manager status/conflict, admin catalog/revision/XSS/media, SMTP notification retry
  and HTTP security all passed. The HTTP-security stack had one transient startup
  failure before the successful clean run; no assertions or limits were bypassed.
- **177 acceptance guards passed**, including three positive/negative port-state
  test methods. The new test file passes Ruff; changed scripts pass formatting.
  Additional script lint has the same 22 pre-existing E501 long SQL/code lines
  as 6991ac7, with zero new diagnostics (backend CI lint remains passing).
- Real review order/replay/manager/SMTP checks passed and both orders survived
  subsequent review releases. Windows trusts the review CA in CurrentUser Root
  with owner permission; an ordinary direct Windows HTTPS request returned 200,
  16 catalog positions and 10 photos. No TLS validation was disabled.
- Original/candidate screenshots use matching desktop/mobile viewports and both
  themes. Final capture/report provenance is stored in the local evidence gallery;
  no API responses are fabricated. Earlier frames are not claimed pixel parity.

Historical `*-synthetic.png` screenshots from the first PR commit remain explicitly
synthetic and are not evidence of original matching or full-stack acceptance.

## Stable adoption after approval

Do not merge or replace stable automatically. Once source/publication decisions,
independent review, required regression/HTTPS acceptance and exact-head CI pass:
use RELEASE-001 to build immutable images from the approved SHA, validate the
manifest and release authority, back up/check restore according to MVP-042,
apply through the existing release controller against the retained stable root,
and repeat HTTPS commerce/chat/manager/media smoke. Preserve catalog, orders,
consent history, audit, photos and volumes. No fresh seed, downgrade, stamp,
volume deletion, manual authority edit or direct development DB connection.

## Continuation on 14 November

The owner clarified that this is product development rather than a management
walkthrough. The previously edited stable catalog is the required assortment.
No new demonstration assortment or demo diagnosis content was inserted into the
manual review. Automated regressions continue to use isolated synthetic fixtures,
as required by AGENTS.md. Original interface tabs and the server-backed wizard
remain in scope unless the owner explicitly removes them.

Original navigation adapters preserve HTTP/SQL/ACL/replay/SMTP assertions.
Browser tests now open the real modal cart, use the Contacts page for independent
support intake, and reload before checking changed media/publication state.
A real browser test also found and fixed informational mode notices interrupting
free-text search. Item deep links and Back/Forward use original-style hashes.

The full real regression exposed a mobile filter defect with many published
models/long labels. The original fixed horizontal radio row clipped those
choices. The adapter now wraps API-backed choices within the viewport; no
model is removed or hidden to satisfy the test. The same shrink fix applies
to the delivery calculator on both item and request pages.

## Acceptance flags

```text
ORIGINAL_ARCHIVE_AVAILABLE=YES
ORIGINAL_DESIGN_MATCH=NO (partial, differences above)
ORIGINAL_PAGES_RESTORED=PARTIAL
ORIGINAL_FUNCTIONS_RESTORED=PARTIAL
EXISTING_CATALOG_PRESERVED=YES (16 published / 10 photos)
LEFT_POST_ON_REQUEST=YES
SERVICE_ICONS=YES
COMMERCIAL_REQUEST_WORKING=YES (real review + replay + manager + SMTP)
ORIGINAL_CHAT_WORKING=PARTIAL (real server; original technical corpus unavailable)
ORIGINAL_WIZARD_WORKING=PARTIAL (server-owned; original technical corpus unavailable)
REAL_BACKEND_INTEGRATION=YES
LOCAL_REVIEW_URL=https://localhost:43351
EXACT_HEAD=recorded in the release manifest and PR verification update
PR_URL=https://github.com/h1roshigoto/akbrizcase/pull/73
```

The current catalog descriptions and privacy bundle are still explicitly
synthetic/closed-demo source data. They were preserved as requested and are not
relabelled as production-approved material. Product readiness requires the
owner's approved commercial descriptions/prices and legal policy through the
existing protected editors/publication workflow. This is distinct from the
working commercial request mechanism verified above.

## Completed local regression and search contract correction

The full isolated release rehearsal on `1f04b3f641f6fca50f5efd089b7e523c20fcbf06`
passed all linked A–E cases: desktop/mobile keyboard, commercial/support writes,
exact replays, manager/SMTP, current-publication applicability, image upload/replace/
remove/revoke, citation withdrawal, expiry, 409/429 and network failures. It then
passed migration-failure preservation, compatible upgrade and rollback, retained
data checks and resource guards (three API workers, zero OOM kills).
This is isolated test data; the manual review retains the owner's 16/10 catalog.

A subsequent actual 3D read found a real frontend/API mismatch: knowledge search
sent limit=20 although the existing backend contract caps results at five. The
client now requests five, validates that contract and advances the document search
using the returned page size. Positive pagination and negative envelope tests are
added. Backend validation remains unchanged. Browser feedback acceptance now waits
for the actual 200 response and cleared pending command before reloading; it no
longer mistakes an already-visible handoff button for committed feedback.

The legacy trainer's model choices and mobile sound/reset grouping now use the
original control classes. This corrects their placement but does not claim the
missing original technical behavior restored.

## Docker Desktop port acceptance

Docker Desktop 29.9.2 lists an unbound PostgreSQL EXPOSE port as a Publishers
entry with PublishedPort=0. The SMTP rehearsal now verifies that summary together
with actual HostConfig.PortBindings and NetworkSettings.Ports on the same daemon.
Every configured or active host binding remains forbidden, including ephemeral
port allocation and loopback bindings. Tests also reject malformed metadata.
The full-release fixture adapter supplies the same Docker-inspection capability.
No application networking, Compose publication, authorization or database schema
was changed by this test compatibility fix.

Final visual comparison aligned the detail photo with the original square frame,
removed inherited inner body/image padding and restored the original title size
and assistant SVG. The redundant pre-form catalog button is removed; ordinary
header navigation preserves the same in-memory form and pending submission,
verified by the existing navigation tests. The receipt's return action remains.
A complete trusted-HTTPS 32-frame capture on 716b803 passed without interception
once the capture waited for all started reads before navigating. A separate
negative capacity probe found that ten simultaneous image reads can return 404
under this bounded review profile; 1/2/6-way reads and an ordinary clean-browser
catalog read passed. AV/file constraints were not relaxed.

## Final review corrective patch (reviewed base f7ee767)

The public scenario contract exposes only `key`, `title` and `model_id`; neither
`ChatScenarios` nor `ScenarioChoice` carries a reviewed symptom relation. The four
original quick choices therefore do not infer a mapping from scenario titles or
keys. Selecting one clears the old server diagnosis and shows a clear unavailable
mapping message, documentation link and the server's specialist action. Both the
local scenario picker and current reply's generic start buttons are suppressed
under that symptom. General published-scenario selection remains available through
the original steps tab after “Начать заново”. No technical instructions are added.

Restart uses existing ChatController commands: leave diagnose for knowledge, then
select diagnose. Each command must be acknowledged before changing the visible
problem/resetting the local picker. A same-mode selection keeps its original
server semantics. On uncertain transport outcome the controller preserves the
exact pending wire for retry; the UI does not relabel old actions as a new
problem. Recover the pending command first, then repeat the choice if needed.
No session is silently recreated and no backend contract/schema is changed.

Six focused UI regressions cover all four unmapped symptoms (including a scenario
with an identical title), progressed restart, shared chat/steps state, lost
transition replay and interrupted reset. The linked HTTPS browser suite adds both
P1 cases on desktop/mobile: progress a published tree, restart, reject unmapped
quick choices, restart an allowed tree, lose a committed transition response,
retry the exact wire and confirm the same response/version; foreign token stays
404. Existing linked HTTP/SQL/ACL/order/consent/SMTP assertions remain.

The image capacity defect is tracked separately in
[#74](https://github.com/h1roshigoto/akbrizcase/issues/74). Reproduction on f7ee767:
ten sequential fresh reads all 200 (0.94–1.40 seconds); ten concurrent reads all
404 (12.87–13.47 seconds). Earlier 2/6-way and normal browser loads passed.
Every image GET runs a fresh isolated scan/decode and maps FileRejected/OSError to
404. Process contention or timeout is a hypothesis, not a proven root cause.
The follow-up requires internal rejection-category/resource evidence, recovery
and publication/revoke regressions without weakening AV, security or limits.
There is no backend performance change in this corrective patch.

Local frontend validation: 254 tests and TypeScript/production build passed.
Exact final-head HTTPS/linked/replay and complete GitHub CI results are recorded
in the PR verification update. Stable release and existing 16 positions/10 photos,
IDs, prices, revisions, consent and publication state are unchanged.
