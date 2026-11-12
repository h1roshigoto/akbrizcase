# MVP-041 — synthetic staging and controlled release

Follow-up: [RELEASE-001 full synthetic staging](RELEASE-001-full-staging.md) adds a
separate format-2 profile and full rehearsal. The format-1 reduced contract below
is retained; its disabled modes and historical receipts do not authorize full staging.

Real target infrastructure acceptance: **BLOCKED**. No target RF server, domain,
public TLS, runtime secrets, network parameters or responsible operator were
provided. Local/CI acceptance is explicitly `accepted-local-rehearsal`; it never
authorizes a public pilot or real personal data. MVP-037/038 legal gates remain
open and ADR-038-A remains PROPOSED. No production deploy, merge or MVP-042 work.

## Supported release profile

`compose.release.yml` is standalone; do not merge it with the development files.
Only loopback TLS ingress is published. Nginx has no data network, application
files or DB credentials. API/worker/PostgreSQL publish no ports. Web/data networks
are internal. Each project has independent labelled database/files volumes,
six independent runtime secrets, ephemeral TLS and explicit CPU/memory/PID bounds.
The controller claims a random `akbriz-staging-<12 hex>` namespace tied to its
private root; existing unrelated containers/volumes are rejected. There are no
external volumes, demo mounts, database DSNs, remote daemon selection or production
endpoints in this path. Local and integration Compose files are preserved.

`modes.json` has no implicit fields or enablement. The initial reduced profile is:

| Mode | Release state |
|---|---|
| Catalog reads | enabled |
| New/replayed orders, chat, knowledge/document reads | disabled, direct HTTP denied |
| Staff mutations except authentication | disabled; existing auth/CSRF/Origin retained |
| Document/image import, delivery and publication | disabled; file pipeline creation denied |
| SMTP | disabled; any nonempty SMTP environment setting fails readiness/startup |
| ClamAV and signature update | `disabled-files-blocked`; no claim of current signatures |
| Real PII | false, cannot be enabled |

Requiring SMTP, scanner or signature update is unsupported and blocks the release;
missing dependencies are never reported healthy. The existing independent CI
SMTP and real ClamAV/sandbox suites continue to verify those implementations.
An enabled file/SMTP release profile needs a separate reviewed extension and
operator-owned credentials/signature supply; this PR does not provision them.
Existing privacy checks are retained: staging does not become a demo/test-marked
database merely through an environment flag. The rehearsal fixture explicitly
marks only its freshly claimed synthetic DB and creates data through existing
seed/domain services from an operator process; HTTP collection stays disabled.

## Identity and trust

`make release-rehearsal` requires a clean checkout, builds backend/frontend from
the exact Git HEAD, and writes `first.json` / `candidate.json` outside Git. Each
manifest has exactly format, full commit, schema and three immutable image refs.
Local application refs are full content-addressed `sha256:<image config digest>`
IDs; these pin all layers through Docker's content-addressed graph. Registry refs
must use `repository@sha256:<manifest digest>` already present in RepoDigests.
PostgreSQL 16.13 is resolved to its actual registry digest at rehearsal build time
and then pinned. There is no mutable tag lookup during release or rollback.

The controller requires a locally known full Git commit, exact image ID/digest,
matching OCI revision and release-gates labels, PG major 16, and the actual
backend schema constant read in a networkless container. A missing digest,
different source label, substituted frontend-as-backend or schema mismatch fails.
This trusts the local builder, Docker daemon and private manifest/receipt storage;
labels are not a signature or third-party provenance attestation. The build
helper is the authorized manifest producer. Untrusted manifests/daemon operators
are outside this boundary. Registry publishing/signing is not implemented.

No application secret is a build argument/context member. Existing allowlisted
Docker contexts and optional ephemeral BuildKit CA mount remain in use. Secrets,
TLS private key, database contents, image archives, runtime logs and private
paths are not CI artifacts. Controller output is closed stage codes only; child
stdout/stderr, DSNs and exceptions are captured and never echoed.

Schema is unchanged: `0011_mvp039_http_security` (one linear Alembic head).
No migration is added by MVP-041.

## Sequence and operations

Run the disposable rehearsal from the clean checkout:

```sh
make release-test
make release-rehearsal
# To select and retain a new private root explicitly (must not exist):
make release-rehearsal RELEASE_REHEARSAL_ARGS='--root /tmp/akbriz-release-review'
```

The root is mode 0700, outside the repository and owned by the caller. Read-only
secret/TLS files in that private parent are mounted only at runtime for non-root
container users. Do not print the root contents or upload it. The self-signed
localhost certificate is clearly **local rehearsal TLS only**, expires after
three days, and never establishes production TLS readiness. Clients verify its
CA and hostname; no insecure TLS flags are used.

For an existing prepared local rehearsal, these are explicit operator operations:

```sh
python3 scripts/release.py check --root /tmp/akbriz-release-review --manifest /tmp/akbriz-release-review/first.json
python3 scripts/release.py apply --root /tmp/akbriz-release-review --manifest /tmp/akbriz-release-review/candidate.json
python3 scripts/release.py rollback --root /tmp/akbriz-release-review --manifest /tmp/akbriz-release-review/first.json
```

The controller holds an exclusive nonblocking lock throughout checks, mutations,
failure handling and durable status writes. Sequence:

1. Preflight: private namespace, secrets, exact supported modes, TLS chain,
   hostname/expiry/key match; no ambient Compose/DB/SMTP override is used.
2. Verify immutable images and render standalone Compose quietly.
3. Start/wait for PostgreSQL; quiesce Nginx, API and worker.
4. Run one explicit migrator container, with only the migrator secret. API/worker
   startup never runs Alembic. Any migration error prevents application startup.
5. A one-shot runtime-role probe checks PG16/schema/least privilege and required
   HMAC/CSRF/release configuration. API readiness includes the release gates.
6. Start/wait for API and worker, then TLS ingress; smoke through verified HTTPS,
   direct disabled-mode requests and actual container port/mount inspections.
7. Only after every check, durably record an accepted manifest and local status.

Failure returns nonzero, records a closed blocked stage and leaves application
services stopped after quiescence. A prior accepted receipt is historical evidence,
never the status of a failed new attempt. Operator recovery is explicit; there is
no automatic fallback to a potentially incompatible image. A power loss leaves
`in-progress`, not a false successful result. This is a maintenance-window release,
not zero downtime or HA.

## Rollback and schema boundary

Rollback requires an exact manifest previously accepted in this private namespace
and the same actual DB schema revision. It quiesces application services, skips
migration entirely, probes the selected old runtime image against the retained DB,
then runs the full HTTPS smoke before acceptance. Secrets, DB and files stay in
place. Never use downgrade, stamp, database recreation or `docker compose down -v`.
If revision compatibility is absent, output is BLOCKED and services remain stopped
pending operator review. Cross-schema rollback needs a reviewed compatibility
contract; there is no guessed revision range or destructive schema revert.

The rehearsal replaces two distinct immutable packaging images of the same source
commit and schema, then returns to the earlier accepted image. It proves the
replacement/rollback mechanism, not compatibility with a historical pre-gates
application or future migration. It compares all retained order/item/history/
consent/outbox rows and the synthetic file across restart, upgrade and rollback.
Fixture data is not a production export and no backup/restore is performed.
Volumes are retained and services stopped at completion, including failure.

## Acceptance and external gates

CI keeps backend, frontend, PG16 integration and Compose/HTTPS/browser jobs and
adds a fifth release job. Every checkout targets the exact PR HEAD. Release tests
cover missing secrets/TLS, absent/substituted digests, unsupported schema/modes,
actual failed migration authentication, DB loss, incompatible rollback, direct
HTTP gates, private mounts/ports, restart/rollback persistence and log canaries.
No deployment credentials, production action or remote persistent store is used.

Real RF infrastructure, domain/TLS/firewall, production secret custody, SMTP,
current authenticated ClamAV signature delivery, operator/incident ownership,
legal retention/full-delete policy and pilot authorization remain **BLOCKED**.
Backup/PITR, file restore and deletion-journal replay belong to MVP-042. RPO <=24h
and RTO <=4h remain unverified until a real timed restore. CI or local rehearsal
success is not evidence of deployment to a real staging server.
