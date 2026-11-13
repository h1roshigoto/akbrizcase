# FRONT-RESTORE-001 — partial implementation; original archive required

Restoration is **BLOCKED**, not complete. Only the two explicitly requested
catalog image exceptions are implemented. The current portal, assistant and
trainer have not been restored to the archive's design or behavior.

Baseline: latest `origin/main` at inspection,
`a7943888409a798d389bba3f1d382cc2cbc3a549` (PR #70 merged).
Branch: `codex/front-restore-001`. PR #71 (`94a85e1`) and PR #72 (`472088a`)
were open Drafts. Neither was merged or cherry-picked. PR #72 overlaps
ItemImage/styles/tests and has a different assistant concept; any later integration
requires explicit review against the original archive.

## Missing inputs

This task runs in `/workspace/akbrizcase`, on a different machine from the
owner's checkouts. Neither `/home/hiros/demo-import` nor `/home/hiros/src` exists
here. Searches of the workspace, attachments, `/tmp` and agent home found no
original RAR/ZIP or `mtdka-site` sources. The only supplied attachment is the
26,830-byte text specification. Previous integration documentation records archive
SHA-256 `860e2bf5d0afca1ff4850f3f741d7af42e639c53f38b2382cb2bc694d4d4eb10`;
that is historical metadata, not an inspected archive in this task.

`curl --max-time 5 -I https://localhost:45767` returned connection refused.
This says nothing about stable availability on the owner's machine. No live
catalog, database, media inventory, PrivacyBundle or diagnostic content could be
inspected. The reported 16 cards / 10 images therefore remain unverified.

To continue, attach the original archive and provide the task on the host with the
development/stable checkouts, or make the approved isolated review environment
and catalog transfer available. Do not send credentials or personal data in chat.
Original HTML/CSS/JS/tests must be inspected before restoring the other surfaces.

## Implemented behavior

- Exact product SKU `ЯИУШ.301421.011-02` displays only **На заказ** in the
  shared image area, including catalog and detail, even when it has a photo.
  Matching is by SKU and product kind, not name or missing photo.
- Services without a usable photo display the existing local technical SVG.
  Their actual valid photos retain priority. Neither visible nor accessible
  missing-image text is used for the service illustration.
- Other products retain the existing fallback. Dimensions, theme colors and
  server image URL validation are preserved. No catalog writes, price-mode
  changes, seeds, backend/admin changes or security changes are included.

## Actual verification

| Check | Result |
| --- | --- |
| `npm --prefix frontend ci --cache /tmp/front-restore-npm-cache --no-audit --no-fund` | PASS; default cache first failed because agent home is read-only |
| `npm --prefix frontend test` | PASS: 235 tests, 14 files; five additional image cases |
| `make frontend-typecheck frontend-build` | PASS |
| `make acceptance-guards` | PASS: 174 tests |
| `node --check` on the three adjusted existing browser runners | PASS |
| `front-restore-images.mjs` with system Chromium | PASS: desktop 1440×1000, mobile 390×844, light/dark; list/detail, unchanged price, other-product fallback, no override photo request, no page errors or horizontal overflow |
| `git diff --check` | PASS |
| Original archive E2E / reference comparison | BLOCKED: archive unavailable |
| Backend/PostgreSQL/HTTPS commercial and linked browser integration | NOT RUN; no full application was provisioned for this partial change |
| CI | Consult the PR's current checks; local results above do not certify CI |

The presentation browser runner intercepts synthetic catalog responses **only
inside Playwright**. It writes no database or product data and submits no orders.
Production code still uses the existing HTTP API with no fixture fallback.
These screenshots do not establish live catalog preservation, original-design
matching, successful order submission or complete security regression coverage.

```sh
npm --prefix frontend run preview -- --port 4173
CHROMIUM_EXECUTABLE=/usr/bin/chromium node frontend/tests/front-restore-images.mjs <<'JSON'
{"origin":"http://127.0.0.1:4173","screenshot_dir":"/tmp/front-restore-images"}
JSON
```

The retained `http://127.0.0.1:4173` preview serves built frontend assets only,
with no API proxy. It is not the requested working full-site review URL.

## Screenshots and changed files

All eight [screenshots](front-restore-001/screenshots/) are explicitly synthetic:
`{desktop,mobile}-{light,dark}-{catalog,left-post-detail}-synthetic.png`.
They show the two image exceptions on the existing portal. Screenshots of the
other original pages and reference/candidate pairs are blocked by missing source.

Changed source/test files:

- `frontend/src/components/ItemImage.tsx`
- `frontend/src/styles.css`
- `frontend/src/test/images.test.tsx`
- `frontend/tests/catalog-media.mjs`
- `frontend/tests/live-catalog.mjs`
- `frontend/tests/mvp046-e2e.mjs`
- `frontend/tests/front-restore-images.mjs`

Also this report and the eight screenshots. Existing image browser assertions
now require the precise service illustration rather than the obsolete service
missing-image text; photo replacement/removal/ACL assertions remain in place.

## Acceptance status

```text
ORIGINAL_DESIGN_MATCH=BLOCKED_ARCHIVE_MISSING
ORIGINAL_PAGES_RESTORED=NO
ORIGINAL_FUNCTIONS_RESTORED=NO
EXISTING_CATALOG_PRESERVED=NO_DATA_WRITES_LIVE_INVENTORY_UNVERIFIED
LEFT_POST_ON_REQUEST=PASS_SYNTHETIC_LIST_AND_DETAIL
SERVICE_ICONS=PASS_SYNTHETIC_LIST_AND_DETAIL
REAL_BACKEND_INTEGRATION=EXISTING_CLIENTS_UNCHANGED_NOT_VERIFIED
COMMERCIAL_REQUEST_WORKING=NOT_VERIFIED
ORIGINAL_CHAT_WORKING=NOT_RESTORED
ORIGINAL_WIZARD_WORKING=NOT_RESTORED
3D_VIEWER_WORKING=UNCHANGED_NOT_VERIFIED
TRAINER_WORKING=UNCHANGED_NOT_VERIFIED
DESKTOP_MOBILE=PASS_IMAGE_EXCEPTIONS_ONLY
LIGHT_DARK=PASS_IMAGE_EXCEPTIONS_ONLY
SECURITY_REGRESSION=NOT_FULLY_VERIFIED_NO_CONTRACT_CHANGES
LOCAL_REVIEW_URL=UNAVAILABLE_FULL_BACKEND_SITE
```

## Stable update after independent review

Do not update stable from this partial branch. Once restoration and independent
review are complete, use the existing RELEASE-001 authority workflow on the
owner's host: exact reviewed commit and immutable image/source manifest, controller
`check`, then authorized `apply` to the existing isolated namespace. See
[RELEASE-001](RELEASE-001-full-staging.md#controller-stages-upgrade-and-recovery) for source checks,
accepted-candidate rollback and retained volumes. Inspect the current root and
manifest before running commands; their paths are unknown on this cloud host.
Never rerun fresh preparation/seed against the existing stable database.
