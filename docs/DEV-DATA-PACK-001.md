# DEV-DATA-PACK-001 — authorized developer catalog packs

Owner: Данил. Issue: #79. This is an explicitly invoked **non-production**
operator tool; it is not registered with FastAPI or ordinary startup.
Real packs and operational evidence are private deliverables, never Git fixtures.

## Approved narrow import contract

The owner authorized a fresh-target-only developer import preserving catalog UUIDs,
SKU, Decimal prices, revisions and selected publication states. Existing CRUD
continues to generate new identifiers/revisions. Production APIs/DTOs, schema,
staff authentication, applicability and image guards are unchanged.

The importer requires the existing full-profile controller witness, matching
explicit target namespace, owner-applied disposable database marker, PG16 runtime
readiness and the current single Alembic head. An environment flag, namespace
argument or database password alone grants no import authority. The operator must
verify newly created namespace-owned DB/files volumes and absence of source/stable
mounts before bootstrap/import. Use the normal release preparation from
[RELEASE-001](RELEASE-001-full-staging.md); never seed an existing working database.
Bootstrap only the fresh database's own synthetic admin, then leave its catalog,
models, orders, consent, chat and KB empty. No source staff identity is copied.

The CLI verifies the active admin with existing Argon2/operator authentication,
including another authorization check after file processing. It accepts no actor
UUID, password argument, force, overwrite, skip-AV or privileged DB role option.
Source data is never queried by this tool. No SQL dump is accepted or executed.

## Format 1

Directory entries are exactly `manifest.json`, `catalog.json` and `images/`.
Archives are unsupported and rejected; inspect any transport archive securely
before creating this directory. No recursive extraction into application storage.

The manifest contains `format=1`,
`classification="authorized-development/non-production"`, a v4 `pack_id`, current
`schema_revision`, provenance, expected counts and exact file SHA-256 checksums.
Provenance records `source_code_commit`, `selective_export_sha256`,
`source_report_sha256`, authorization scope and a safe authorization reference.
These assertions must also be checked against the owner's actual source/rights
records. A self-asserted manifest/checksum is not proof of permission/authenticity.

`catalog.json` has exactly `catalog_items`, `equipment_models` and an empty
`catalog_item_models`. Items reuse the current `ItemCreate` field validation plus
v4 `id`, `status`, positive bounded `revision` and nullable `image_file`. Prices
are decimal strings with exactly two places or null; floats/exponents/rounding are
rejected. Models reuse `ModelFields` plus v4 `id` and `status`. Every image path
is `images/<item-uuid>.jpg` or `.png`, with bytes bound to that item. There are no
source storage keys/receipts/timestamps, staff, orders, contacts or auth history.
Extra fields/files, duplicates, symlink/hardlink files, traversal, bad checksums,
counts/UUIDs/SKU/prices/statuses and incompatible service shipping are rejected.
Attribute objects are bounded by the existing DTO and reject sensitive key names.
The owner must review free-form text/attributes for allowed content and rights.
The private delivery instructions belong alongside the pack, outside its strict
validated directory. Use only wholly artificial data for public tests.

## Validation and import

In the repository's locked Python 3.12 environment:

```sh
cd backend
uv run --locked python -m app.modules.catalog.developer_pack validate --pack /private/developer-pack
```

Prepare an independent full-profile target with the existing controller; do not
run catalog/orders seeds. Verify actual volume/network/secret/CA isolation before
any writes. Stage a private copy of the checked pack as runtime UID 10001, regular
0600 files under 0700 directories, and mount that copy **read-only** as operator
input. Never copy it into verified storage. On the validated target Compose
environment, run the CLI in a real protected terminal with the image's runtime
identity and existing AV composition:

```sh
python -m app.modules.catalog.developer_pack import --pack /operator/developer-pack --target-project TARGET_PROJECT
```

Enter that new target's admin login/password using hidden prompts. Follow the
existing [operator terminal pattern](RELEASE-001-full-staging.md) and AV profile;
do not add new scanner overrides or change production ClamAV settings. For the
controller-selected synthetic fixture only, the already accepted
`/opt/release/browser_media_server.py` supplies `release_scanner()`; invoke this
CLI inside that existing context, as the full-release rehearsal does for file
operations. Direct CLI execution with the signed-CVD composition is unchanged. The
controller-selected synthetic AV composition is only a closed rehearsal and
does not certify production signed-CVD coverage or capacity.

Every image goes through existing quarantine, input scan, isolated decode/re-encode,
output scan and fresh preparation. New keys and genuine local receipts are created
by `ImagePipeline`. Source/served SHA-256 values are reported separately: JPEG
re-encoding may change bytes. Never infer byte parity from visual similarity.

All model/item bindings, original revisions/statuses and a **new local** audit
receipt commit in one shared UoW. File processing precedes DB locks; staff and
catalog locks serialize final authorization/binding. Exit integrity checks run
before commit. A failure leaves no successful DB receipt or partial catalog;
private unbound/quarantine objects may remain. Preserve them for inspection and
retry the same validated input; do not delete working data or fabricate receipts.

Reimport verifies the original pack digest, all rows/fields/keys and freshly
prepared served-image hashes, returning `ALREADY_IMPORTED` without writes.
Conflicting UUID/SKU, changed rows, extra catalog records, changed source pack,
missing local receipt or altered image bindings refuse the entire operation.

## Acceptance and limits

Check trusted HTTPS `/healthz`, `/readyz`, catalog list plus every detail, models,
and **sequential** image GETs through the actual backend. Compare all allowed
fields, UUID/SKU/Decimal/revision/status, services/shipping, source/served hashes,
and public React rendering. Restart only the owned disposable services and repeat
the same checks. Preserve the source and stable unchanged. Reproduction and actual
results, including error injection/negative tests, are recorded in the Issue and
private task report; the document itself is not a runtime PASS receipt.

This format refuses all KB/applicability dependencies. Models and items remain
available, while applicability stays `unknown`; no expert, source document,
publication attribution or review reference is invented. Synthetic consent/teaching
content is separate. No new migration, infrastructure configuration, frontend
driver, API contract or fix for #74 is included. #75/#76 remain Alexander's work.
His independent machine import is accepted only after his confirmation.
