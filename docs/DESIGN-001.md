# DESIGN-001 — bounded design foundation

Baseline: `a7943888409a798d389bba3f1d382cc2cbc3a549`.
Branch: `codex/design-001-foundation`. Product inspection, dependencies,
onboarding strategy and P0–P3 roadmap: [PRODUCT-001](PRODUCT-001.md).
Final exact SHA and CI snapshot are recorded in the Draft PR delivery report;
a commit cannot contain its own SHA.

## Design direction and scope decision

Keep АК Бриз navy, locally bundled Golos, original logos, restrained surfaces
and both themes. Equipment/SKU/model and verified applicability should lead the
information hierarchy, followed by price status and the commercial next action.
Use aligned readable facts and tabular numbers where relevant; do not decorate
technical work with unsupported claims or invented product photography.
Responsive composition uses the existing 1000/640px breakpoints. WCAG 2.2 AA is
the target; this slice supplies focused evidence, not whole-product certification.

The existing architecture already has suitable page shell/grid components,
shared catalog feedback and native controls. A new component framework/router,
global redesign or form state rewrite would add risk without solving this slice.
Keep the existing CSS class contract and introduce opt-in React/CSS primitives.

Implemented:

- `frontend/src/design/tokens.css`: existing light/dark tokens extracted verbatim
  from imported design CSS; additive rem spacing/type/line-height/control scales,
  content measure and semantic control-border/focus/error tokens. Legacy variable
  names remain usable. No business palette values are fabricated.
- `design/controls.css`: the pre-existing primary/secondary/add button rules have
  one home and use scale tokens at their previous dimensions. Legacy buttons
  continue working. Enhanced border/focus styles apply to the catalog pilot.
- `components/Button.tsx`: typed native button, default `type=button`, explicit
  `type=submit`, primary/secondary variants, native disabled/ARIA/events/ref props.
  Adopted only by search, pagination and catalog state recovery actions.
- `catalog-controls`: stronger field boundaries and visible keyboard focus for
  existing search/select/filter controls, with forced-colors support.
- `ui-stack`: small CSS layout primitive; catalog `StatusPanel` adopts a consistent
  space/type scale, atomic live announcement, text plus warning icon for errors,
  explicit retry/reset, and reduced-motion behavior inherited from existing CSS.
- New positive/negative unit tests and a real-API browser acceptance runner.

The shared legacy control rules move without changing their default values;
visible changes are scoped to catalog controls/status. Remaining pages keep their
current markup. No API/backend/schema/security changes. Existing prices,
compatibility, request retry/ownership and server diagnosis remain authoritative.
Do not generalize StatusPanel's catalog-specific messages to other domains yet;
future adoption should first define domain-appropriate content and semantics.

## Acceptance and verification

| Check | Actual result |
| --- | --- |
| Frontend Vitest | 236 passed, 15 files, including 6 new foundation tests |
| TypeScript and Vite build | PASS after final CSS adjustment; CSS 66.06 kB, JS 340.69 kB (gzip 14.54/104.29 kB) |
| Backend unit/contracts | 1245 passed, 1723 deselected using the configured non-integration selection |
| Backend lint/format/types | Ruff PASS, 266 formatted files; mypy PASS, 248 source files |
| Repository acceptance guards | 174 passed |
| Real PostgreSQL commercial regression | 64 passed, 86.99 s; original transaction/replay/consent/status/SMTP/privacy cases preserved |
| Baseline browser | 6 PASS: 1440/768/390 widths × light/dark; real API, loading, empty/reset, transport error/retry, keyboard, no overflow |
| DESIGN-001 after browser | 6 PASS on implementation `8f6d3ca57d01f8c332aeaef91003eea500f2f31b`: both themes × three sizes, contrast/focus/44px/forced colors included |
| Existing full visual smoke | PASS: desktop/mobile both themes, WebGL lifecycle/fallback, cart keyboard, form validation, documents, chat/wizard and trainer |
| Full GitHub Actions | Triggered by Draft PR; actual per-job snapshot is in the PR delivery report, no performance PASS inferred from local checks |

The initial ad-hoc backend container run failed 101 tests because its image does
not include the shared frontend contracts/root scripts. Repeated with the complete
repository mounted read-only at `/workspace`: all 1245 passed. No test or guard
was weakened. The initial browser runner selector ambiguously matched two reset
buttons; scoped recovery to the actual status panel, then all baseline cases passed.
The first existing visual smoke reached the Node HTTP client without the development CA; adding `NODE_EXTRA_CA_CERTS` to the disposable browser runner resolved it and the full smoke passed. Host-native browser install was unavailable; the container runner described below
resolved that toolchain problem with certificate verification retained.

Local checks with the pinned Node 24.19.0/npm 11.9.0 toolchain:

```sh
npm --prefix frontend test
make frontend-typecheck frontend-build
make acceptance-guards
python3 scripts/db_integration.py --suite commercial-regression
```

Backend verification used the project's Dockerfile integration target with its
Python 3.12.14/locked uv environment. Mount the complete checkout read-only,
work in `/workspace/backend`, use `/app/.venv/bin` tools, disable pytest/ruff
repository cache and place mypy cache in `/tmp`. Execute the same pytest
non-integration selection, Ruff check/format and mypy as the Makefile.
The commercial runner builds its own fresh labelled PG16 namespace and preserves
its volumes after stopping. That run recorded the baseline HEAD with dirty=true
because frontend/docs edits were underway; backend was unchanged. This is local
regression evidence, not exact-final-commit CI evidence.

Browser acceptance input (normal supported host with Chromium and trusted CA):

```sh
printf '%s\n' '{"origin":"https://localhost:35067","phase":"after","screenshot_dir":"/tmp/design-001"}' \
  | node frontend/tests/design-foundation.mjs
```

For this host, `/tmp/akbriz-product-browser/run.py before|after` wraps the same
script in `akbrizcase-product001-browser:local`, derived from the official pinned
Playwright 1.58.2 Noble image, adding NSS tools. It mounts only the public CA,
read-only repository and writable screenshot directory, with a fresh private NSS
store inside the disposable container. It uses host networking only to reach the
local dedicated synthetic HTTPS endpoint. Neither production credentials nor MVP
volumes are mounted. The base runner includes Node 24.13.0; browser code executes
with the pinned Playwright package from the project's node_modules. Unit/build
checks use the required Node 24.19.0/npm 11.9.0.

Browser tests deliberately hold the real catalog request for loading assertions
and abort transport for the negative case. Recovery continues real requests;
there are no successful fixture responses, order POSTs or staff mutations. After
checks add computed normal-text ≥4.5:1 and control/focus ≥3:1 contrast, ≥44px
primary-button height, keyboard focus, and forced-colors checks. These assertions
cover the pilot only, not the entire portal or all WCAG success criteria.

Baseline screenshots were captured from the immutable baseline frontend running
in the separate synthetic development stack, not from the existing MVP. The
before/after fixtures, viewport, theme and fault scenarios are identical.

| View | Before | After |
| --- | --- | --- |
| Desktop light catalog | [before](design-001/screenshots/before-desktop-light-catalog.png) | [after](design-001/screenshots/after-desktop-light-catalog.png) |
| Desktop light error | [before](design-001/screenshots/before-desktop-light-error.png) | [after](design-001/screenshots/after-desktop-light-error.png) |
| Mobile dark catalog | [before](design-001/screenshots/before-mobile-dark-catalog.png) | [after](design-001/screenshots/after-mobile-dark-catalog.png) |
| Mobile dark error | [before](design-001/screenshots/before-mobile-dark-error.png) | [after](design-001/screenshots/after-mobile-dark-error.png) |

## Limitations and release boundaries

Synthetic development only. Local controller acceptance is not a production
readiness claim. The reported button p95 540.29 ms versus 300 ms and API memory
peak at 512 MiB remain open; thresholds and limits are unchanged. Full signed-CVD,
real legal/content/company rights and operator/infrastructure admission are still
separate gates. No original catalog data was imported. The development and stable volumes,
networks and bind sources are disjoint. Stable containers were discovered stopped
at 09:07 Istanbul during this task, with cause undetermined; checkout and volumes
are retained. No task stop/start/recreate targeted that namespace. Availability
of the stable MVP is an explicit unresolved operational issue, not an isolation PASS.
See [isolation evidence](design-001/isolation.json). See PRODUCT-001 for detailed acceptance criteria and next tasks.


## Changed files

Implementation:

- `frontend/src/components/Button.tsx`
- `frontend/src/components/Icon.tsx`
- `frontend/src/components/StatusPanel.tsx`
- `frontend/src/design/tokens.css`
- `frontend/src/design/controls.css`
- `frontend/src/design/assets/design.css`
- `frontend/src/pages/CatalogPage.tsx`
- `frontend/src/styles.css`

Tests:

- `frontend/src/test/designFoundation.test.tsx`
- `frontend/tests/design-foundation.mjs`

Documentation/evidence:

- `docs/PRODUCT-001.md`
- `docs/DESIGN-001.md`
- `docs/design-001/isolation.json`
- `docs/design-001/screenshots/before-desktop-light-catalog.png`
- `docs/design-001/screenshots/before-desktop-light-error.png`
- `docs/design-001/screenshots/before-mobile-dark-catalog.png`
- `docs/design-001/screenshots/before-mobile-dark-error.png`
- `docs/design-001/screenshots/after-desktop-light-catalog.png`
- `docs/design-001/screenshots/after-desktop-light-error.png`
- `docs/design-001/screenshots/after-mobile-dark-catalog.png`
- `docs/design-001/screenshots/after-mobile-dark-error.png`
