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
- Current frontend: 246 tests passed; TypeScript and production build passed.
- Unchanged original reference: **106/106 original HTTP E2E passed**, using the
  repository’s pinned Playwright package. This certifies the reference, not candidate parity.
- PostgreSQL run encountered exhausted Docker address pools. Exactly eight unused
  networks from two stopped disposable rehearsal namespaces were released after
  checking zero endpoints; stable and PR #72 review networks, all containers and
  all volumes were retained. The exact-head GitHub PostgreSQL 16 integration job passed on a44e8e1; subsequent exact-head reruns remain to be checked.
- Initial backend container run lacked sibling frontend/scripts paths and failed
  on missing test inputs; the corrected read-only whole-repo run passed: 1166 tests, Ruff and mypy.
- 32 real-backend candidate viewport screenshots were captured without API mocks, alongside 32 original frames. Visual audit found mobile hero clipping and an undersized delivery button; both are corrected in the continuation. Capture readiness is being improved before final evidence. Manual review commerce/support, replay, manager and actual SMTP checks passed. The full linked regression and exact-head CI remain pending: earlier runs stopped on original-UI selector adaptations; no business/security assertions were removed.

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

The previous exact-head CI run passed backend, frontend, PG16 integration, encrypted
restore, load/recovery and operator handover. Three jobs failed on obsolete UI
selectors/text after restoring the original interface. Their interactions are
being adapted to the original live search, modal cart, model buttons and quick
actions; HTTP/SQL/ACL/replay/SMTP assertions remain. A real newly added test also
found and fixed informational mode notices incorrectly interrupting free-text
search. Deep links and browser Back/Forward now use original-style item hashes.
