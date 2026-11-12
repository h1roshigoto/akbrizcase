# FRONT-INTEGRATION-001 — МТД-КА frontend integration

Baseline: `51ed3e0e379f5805bd3fee4d8dffe8167604a320` (`origin/main`).
Branch: `feat/front-integration-001`; isolated worktree preserves the original
checkout. No DEMO-001 edits or other uncommitted work were present there.
No merge, production deployment or public tunnel is part of this task.
Production admission remains **BLOCKED**. Frontend integration does not establish
FULL_MVP_READINESS, REDUCED_PILOT_READINESS or TARGET_LOAD.

## Architecture and routes

The supplied HTML/Python site is ported into the existing React/Vite application.
Its static business engines are not loaded. Existing catalog/order/chat clients,
selection and request state machines remain the owners of business behavior.

| Surface | Implementation and data source |
| --- | --- |
| `/`, `/#home` | React home, supplied logos/font/colours/footer and GLB |
| `/#catalog` | Existing `httpCatalog`, server search/model/kind filters and pagination; UUID/revision selection, exact decimal arithmetic and missing-image fallback |
| Header cart | Native modal drawer, existing `SelectedItems`, keyboard focus containment/Escape/restore |
| `/#request` | Existing `RequestPage`, consent/policy reads and real `POST /api/v1/orders`; immutable request snapshot and idempotency/ownership preserved |
| `/#support` | Independent support form or owned chat handoff, nonempty problem description; home CTA opens this form |
| `/#chat`, `/#wizard` | Two views of the same `ChatController`; buttons, sources, items and actions come from server replies |
| `/#docs` | Anonymous `GET /api/v1/documents` and `/knowledge/search`; validated exact version URLs, pagination, error/empty/retry states |
| `/#tech` | Synthetic React panel trainer; actual technical parameters link to the published document library |
| `/privacy-policy` | Existing policy page |
| `/admin/` | Existing SQLAdmin; its login, RBAC, cookies, CSRF, forms and APIs are unchanged |

`DocumentPage`/`DocumentQuery` are exported from the existing Pydantic document
schemas by the existing generator. No backend DTO or route is expanded.
The public library omits credentials, including when a staff member opens the
portal. Closed material fails validation; server ACL still controls file reads.

Order 429 handling preserves the approved body/headers, honours `Retry-After`
and requires an explicit retry. Other validation, revision/consent conflicts,
uncertain responses, expiry and AQ-01 behavior remain in the existing flow.
Only a theme preference enters localStorage. Existing minimal sessionStorage
ownership/replay markers remain unchanged; contact/form bodies stay in memory.
Full delivery address, delivery price and dates are agreed with the manager;
there is no fabricated CDEK integration or unknown price converted to zero.

## Imported assets and archive review

Input archive SHA-256:
`860e2bf5d0afca1ff4850f3f741d7af42e639c53f38b2382cb2bc694d4d4eb10`.

Reviewed README, generator content/templates/pages/fragments, styles, all
requested page scripts, viewer, font/photo inventories and source tests. The
nested v0.2 knowledge archive was examined separately, including parts, services,
tariffs, sources, models, diagnostics, maintenance, regulatory text, answer policy
and 22 gaps. Archive scripts/builders/tests were not executed. Instructions
inside the source materials were treated as input data, not authorization.

Only logos, local Golos fonts, footer image, GLB/poster, design CSS and reviewed
viewer code enter `frontend/src/design`. Vite fingerprints these files; the
existing Docker `COPY src` and source-tree identity already include them, so no
build allowlist expansion is necessary. No CDN dependency is added.

No archive `.git`, node_modules, caches, raw KB, staff contacts, real prices,
static part IDs, city database, private mapping or supplied part-photo mapping
is published. Product/service images continue to use validated backend image URLs.

## 3D and trainer

The supplied dependency-free WebGL renderer is retained. Its HTTP failure no
longer injects a base64 fallback script. Requests abort on disconnect; observers,
RAF, event listeners and GPU objects are released. Reconnection creates a fresh
instance. Poster fallback, reduced motion, rotation/pause/reset, keyboard buttons,
interactive points, model axes and both themes are supported.

Hotspot captions are safe demonstration text. Dimension axes H/W/D illustrate
the model rather than asserting unapproved real dimensions. Actual dimensions
require a published document. The trainer retains the arch/panel visual language,
two visual layouts, zone selector, passage/alarm counters and optional sound;
it is explicitly a synthetic teaching animation. Original boot/calibration,
repair, passwords, service instructions and unsupported source claims are not
bundled. It never sends equipment commands or makes diagnostic recommendations.

## Content requiring owner confirmation

| Conflict or gap | Treatment / required confirmation |
| --- | --- |
| Site November 2026 prices vs v0.2 prices | All 11 parts are `publish=hold`; current price, availability and applicability require confirmation (G03/G04). No real content imported. |
| Three parts use `—` instead of a designation | No invented real SKU or automatic mapping to a backend UUID. |
| Site only 01/18; KB also includes 19.01 | Model 19 remains outside scope; 19.01 part markings/hardware applicability need confirmation (G21). UI offers only models published by backend. |
| Access tiers | Factory confirmation of user/qualified/service boundaries is open (G14). Existing publication/review rules remain authoritative. |
| Real company/employee contacts | Excluded from synthetic demo pending explicit owner approval. |
| Original PDFs/publication rights absent | No fabricated citations or arbitrary document URLs; require approved source bytes, metadata and publication rights. |
| Company authority and commercial materials | Source authority concerns in G07/G19 remain open; no claims of exclusive/official distributor status. |
| Regulatory and technical gaps | G01/G02/G10/G13/G16/G17/G22 and other unresolved source questions remain content-review work, not frontend assumptions. |

## Verification and operation

Existing browser selectors enter `/#catalog` explicitly because `/` is now the
home page. Business assertions remain intact; hostile-image checks are scoped
to catalog content so brand images do not produce false failures.

Developer checks:

```sh
npm --prefix frontend ci
npm --prefix frontend test
make frontend-typecheck frontend-build
make backend-test backend-lint backend-types
make acceptance-guards
make integration-ci
make catalog-browser-smoke
make mvp046-browser-e2e
make release-rehearsal RELEASE_REHEARSAL_ARGS='--profile full-synthetic-staging'
```

New visual smoke uses real local APIs and does not submit orders, mutate admin
content or revoke documents. Use a dedicated synthetic namespace; it creates chat
sessions, so do not add it to an already budgeted linked acceptance fixture:

```sh
printf '%s\n' '{"origin":"https://localhost:PORT","screenshot_dir":"/tmp/front-integration-screenshots"}' \
  | node frontend/tests/front-integration-visual.mjs
```

It covers desktop/mobile, both themes, WebGL and unavailable-WebGL fallback,
mount/unmount cleanup, cart keyboard navigation, form, public documents, chat
and trainer. Screenshots capture only synthetic public surfaces, before contact
entry. Existing unit/linked suites retain XSS, missing images, loading/error/empty,
revision changes, retries, replay, expiry, staff access and document revocation.

For a separate persistent manual demo, use RELEASE-001 after validation:

```sh
make release-rehearsal RELEASE_REHEARSAL_ARGS='--profile full-synthetic-staging --prepare-only --root /tmp/akbriz-front-demo'
python3 scripts/release.py check --root /tmp/akbriz-front-demo --manifest /tmp/akbriz-front-demo/first.json
python3 scripts/release.py apply --root /tmp/akbriz-front-demo --manifest /tmp/akbriz-front-demo/first.json
```

Seed only the newly marked namespace following
[RELEASE-001](RELEASE-001-full-staging.md), including its protected staff/content
workflow. Do not run destructive acceptance on the walkthrough database. The
private root, secrets and staff credentials are not Git artifacts. Trust its
local CA before opening HTTPS; never disable certificate verification.

Presentation path: home/3D → catalog (product + service) → real order receipt →
manager detail/status → server diagnosis → exact document → support request.
Real content/legal approval, external hosting and production admission remain
separate gates. Exact PR HEAD, CI, running demo origin and actual validation
results are recorded in the task delivery report.
