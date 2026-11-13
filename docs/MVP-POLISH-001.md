# MVP-POLISH-001 — local synthetic presentation

Stable: <https://localhost:45767>. Visual/UX review: <https://localhost:34203>.
Production admission and external access remain **BLOCKED**.

The final local delivery packet, exact delivery HEAD, PR URL and CI snapshot are
saved in `.delivery/REPORT.md` in this worktree, outside `/tmp`. This document and
`mvp-polish-001/` hold the shareable implementation and acceptance evidence.

## Operational recovery on stable

The stable checkout was clean at `a7943888409a798d389bba3f1d382cc2cbc3a549`.
`origin/main` still had that SHA on the compatibility refresh. PR #71 was an open,
unmerged Draft at `94a85e1ce8ece5ffb0871dca3297e10f0de03983`; it was not merged or
used as a dependency. Stable source, images, release profile and ports were not
changed. Its 17 catalog records (16 published), two staff users, existing chat
sessions, files and volumes were retained.

Before publication, genuine trusted-TLS Chromium requests returned 503 from both
`/api/v1/privacy-policy` and `/api/v1/consent-text`, with safe
`SERVICE_UNAVAILABLE` codes. PostgreSQL contained zero consent texts and zero
privacy publications. `/privacy-policy` itself returned the SPA HTML with HTTP
200; its document API failed. This distinction corrects the earlier reported
page-level 503. `documentsReady` was false because the documents were unavailable;
`ready` also requires an editable form, an API source, a valid selection and
current revisions. Native disabled controls caused the block. After recovery,
actual mouse and keyboard operation passed, with pointer-events unchanged.

The owner ran the protected privacy CLI through `scripts/polish_privacy_operator.py`.
`--expect-empty` was used only after reading **both** empty lifecycle tables.
The one-shot container received migrator credentials; API and worker did not.
Exact stable authority/namespace/image, schema, admin authentication and the
existing publisher guards remained in force. No seeding or direct SQL mutation
was used on stable. Standard `check --bundle` returned:

```
VALID_PACKET 31371cd94e0805a7f9f76f9136cc11efc4a8b12817d8767dafc7c79fc321d217
```

The bundle is explicitly `SYNTHETIC ONLY`, not legally approved. No company legal
identity or requisites were invented. Consent is `SYNTHETIC-POLISH-001-v1`;
policy is `SYNTHETIC-POLISH-POLICY-v1`. Their linked consent version matches and
collection is enabled only by the existing local synthetic authority/DB marker.
The actual text was opened and checked in the request form.

A real commercial request **№1** was created through the browser (HTTP 201):

| SKU | Quantity | Unit price | Revision snapshot |
| --- | ---: | ---: | ---: |
| ЯИУШ.301421.011-02 | 1 | 110000.00 RUB | 3 |
| DEMO-SRC-V01 | 1 | 7000.00 RUB | 3 |

Known/items subtotal: 117000.00 RUB. Delivery remains `pending_quote`; delivery
and total amounts are unknown. Exact logical replay returned HTTP 200 and the
same number; PostgreSQL contained one commercial order. Consent evidence,
original snapshots and initial `new` status history were verified independently.
The existing creation service records initial history, consent and outbox; it
does **not** emit a separate `order.create` AuditEvent. No such event is claimed.
Privacy/content publication audits were verified. SMTP outbox was `sent`, one
attempt; the isolated sink received the notification for №1 with `.invalid`
recipient and local administrative URL.

The owner used hidden-input Chromium staff acceptance to view the same order as
admin and manager. Both reached and checked the original item snapshots and
consent version; screenshots are retained. The supplementary helper originally
asserted the wrong statistics permission, then called the API route and received
401 after the successful order checks. The helper now uses the administrative
route. Its corrected complete staff run is **NOT CLAIMED PASS**. The successful
order-view checks and screenshots are separate evidence. Cookie Path is `/`;
no cookie scope, RBAC or authorization rule was changed.

## Assistant recovery and content

Initial published counts were models=2, trees=0, public documents=0,
assistant-allowed sections=0. Chat modes were enabled by the full synthetic
profile. The missing privacy publication blocked new collection/exchanges;
missing published content independently prevented diagnosis and knowledge hits.
After privacy publication, session creation returned 201 and model/scenario
queries returned 200, with empty scenario lists for both models.

The original RAR and its `mtdka-site/assets/kb.js` were inspected as data, without
executing archive code. The `(1)` archive variant was absent. The KB has seven
source records, eight MTD-KA-18 groups/28 causes, five MTD-KA-01 sections and eight
symptoms. These real service actions require separate rights, source safety and
expert review. None was imported or published. See
[machine-readable source assessment](mvp-polish-001/source-assessment.json).

The owner explicitly reviewed the generated teaching text and both paths and
ran `scripts/polish_content_operator.py` with hidden admin authentication. It
used the existing import → check → preview → review → publish services,
DiagnosticTree check/import/preview/publish, private checksum-bound evidence,
public ACL and `assistant_allowed`. The one-shot runtime operator used the
existing signed CVD files and real ClamAV, without replacing the API scanner or
changing its release composition. Existing documents/sessions were not seeded,
reset, overwritten or removed. All content/review references say SYNTHETIC ONLY.

The published MTD-KA-01 scenario asks only whether DEMO is visible on a teaching
screen. One path returns a synthetic result and feedback; the other hands off
to a specialist. It contains no real repair instruction, part recommendation or
compatibility assertion. MTD-KA-18 intentionally has no scenario.

Fresh reads of stable now show:

```
PUBLISHED_EQUIPMENT_MODELS=2
PUBLISHED_DIAGNOSTIC_TREES=1
PUBLIC_KB_DOCUMENTS=1
ASSISTANT_ALLOWED_SECTIONS=1
```

Browser acceptance completed the diagnosis/result/positive-feedback path,
reloaded the same session, switched chat/steps without creating another session,
verified the unsupported model, searched the published teaching document,
opened its real file (HTTP 200), searched the exact left-post SKU and opened its
card. Specialist handoff opened a review form with no automatic POST. A separate
explicitly confirmed synthetic support request **№2** then returned HTTP 201,
`source=chat`, no items, with the owned chat link. No staff credentials, session
or receipt tokens were saved in evidence.

## Frontend changes

- `ItemImage` keeps the existing validated same-origin image URL and real-image
  priority. Missing/failed service images use a local neutral SVG illustration.
  The actual exact SKU `ЯИУШ.301421.011-02` alone uses «На заказ» and «Поставка по
  согласованию». No delivery/availability field exists in the public DTO or item
  schema; arbitrary attributes are display data, not an approved stock contract.
  This narrow presentation exception changes no price, price mode, stock,
  compatibility or delivery-date business data. Other missing product images
  retain their existing fallback.
- Request recovery explains why document failure blocks both consent and sending.
  Existing retry, version equality, collection-enabled gate, revisions,
  idempotency and explicit unchecked consent/confirmation remain unchanged.
- «Технический помощник» explains three tasks, offers model/problem/questions/
  result guidance, and provides actual documentation/specialist alternatives
  when a model has no scenario. Session controls use clearer language. The
  existing controller, server trees, ownership and exact-retry behavior remain.
- Existing browser image assertions now distinguish service illustrations from
  absent product images; counts/real-image/replace/remove checks are retained.

## Review isolation and limits

Namespace `akbriz-staging-b920d22c96bb` has its own PG16 database, files volume,
networks, six fresh credentials and TLS root. Its only published port is
`127.0.0.1:34203`; it has no connection to stable's DB or volumes. Existing
PR #71 development at `https://localhost:35067` was retained independently.
See [state/isolation evidence](mvp-polish-001/state.json).

Review re-onboarded the 16 original catalog field sets through authenticated
CatalogWriteService into its initially empty owned DB, retaining the closed-demo
warning and prices. **Original product photos were not imported into review.**
Stable photos/files were not changed. Review has an independently published copy
of the synthetic teaching document/tree. It is not a database clone or a new
accepted stable release. Its CA is `/tmp/akbriz-polish-001-review/ca.crt` and must
be trusted for manual review; browser acceptance used a private trusted NSS
store and verified TLS without ignore flags. Runtime root is under `/tmp`;
this durable report and screenshots are outside it.

## Actual validation

| Check | Actual result |
| --- | --- |
| Frontend unit/regression | 239 passed in 14 files; nine additional image, document recovery and assistant tests |
| Frontend typecheck/build | PASS, Node 24.19.0 / npm 11.9.0 |
| Backend non-integration | 1245 passed; Ruff check/format and mypy PASS |
| Acceptance guards | 174 passed |
| PostgreSQL integration | All suite checks passed across full run plus targeted HTTP-security rerun; see details below |
| Commercial regression | Separate initial run 64 passed; full integration's 64 also passed |
| Config / Compose build / smoke | PASS; separate owned smoke namespace stopped, volumes retained |
| Stable real browser | Commercial creation/replay, actual stored snapshots, explicit checkboxes, diagnosis, knowledge, commerce, handoff and confirmed support creation PASS |
| Review real browser | Catalog/detail at 1440 and 390 in both themes; five services, exact left-post SKU, assistant paths PASS |
| Review failure paths | Scenario transport error/retry, byte-identical message retry, missing/wrong session token 404, policy fail-closed/recovery without auto consent PASS |
| Linked MVP-046 | Final outcome recorded in the durable delivery packet; not inferred from standalone tests |
| GitHub Actions | Exact delivery-head status/link recorded in the durable packet; not inferred from local results |

Full PG run: clean upgrade 1; core 1051 (2 existing skips); images 58; lifecycle
43; applicability 75; trees 137; chat 180; commercial 64; ACL publication 65; KB
acceptance 37. HTTP-security initially had 10 pass/1 setup error: a Uvicorn did
not become ready within the unchanged 15-second deadline. Its complete targeted
rerun passed 11/11. The initial `make integration` command therefore exited 1;
the rerun is reported separately, with no assertion/deadline change.

The original dev linked harness first stopped before browser assertions under
host Python 3.14's CA validation, then under its unchanged Compose private-port
guard. Supported Python 3.12 and the existing full-release linked harness were
used for the final linked run. No CA validation or port guard was weakened.

## Screenshots

- [Left post, desktop/light](mvp-polish-001/screenshots/review-stand-desktop-light.png), [mobile/dark](mvp-polish-001/screenshots/review-stand-mobile-dark.png).
- [All five services, desktop/light](mvp-polish-001/screenshots/review-services-desktop-light.png), [mobile/dark](mvp-polish-001/screenshots/review-services-mobile-dark.png). Individual service/detail/theme variants are in the same directory.
- [Enabled consent/confirmation and actual policy text](mvp-polish-001/screenshots/stable-request-enabled.png).
- [Commercial request №1 receipt](mvp-polish-001/screenshots/stable-order-created.png).
- [Same request in admin/orders](mvp-polish-001/screenshots/stable-order-admin.png), [manager view](mvp-polish-001/screenshots/stable-order-manager.png).
- [New assistant directions](mvp-polish-001/screenshots/review-assistant-directions.png), [equipment and published scenario](mvp-polish-001/screenshots/stable-assistant-model.png).
- [Real diagnostic question](mvp-polish-001/screenshots/stable-diagnosis-question.png), [result](mvp-polish-001/screenshots/stable-diagnosis-result.png).
- [Explicit support confirmation](mvp-polish-001/screenshots/stable-support-confirmed.png), [support receipt №2](mvp-polish-001/screenshots/stable-support-created.png).

`before-*.png`/`before.json` are transitional loading observations after privacy
publication, not screenshots proving the earlier pre-publication 503. The earlier
503 evidence is the initial real Chromium API result and empty lifecycle reads.

## Remaining owner decisions

Independent review and explicit authorization are required before updating stable
code. PR #71 remains separate. Real company/legal documents, manufacturer repair
instructions, expert approval, real PII and external/production access remain
outside this synthetic demonstration. No merge or external deployment occurred.
