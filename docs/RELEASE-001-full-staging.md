# RELEASE-001 — isolated full synthetic staging

Engineering follow-up to MVP-041, based on
`048bb7ef1cfe47322f4fdd79173a21a1c57db839`. No production profile, deployment,
public pilot, real contacts or target-load authorization is introduced.

## Explicit profiles

| Contract | Reduced synthetic rehearsal | `full-synthetic-staging` |
| --- | --- | --- |
| modes format | 1, original fields | 2, mandatory explicit profile and all fields |
| Catalog | original catalog flag | enabled |
| Orders/replay, chat, knowledge, documents/images | disabled | enabled through existing domain services |
| Staff/catalog/content mutations | closed except authentication | existing DB authorization, RBAC, Origin, CSRF and audit |
| SMTP | disabled; nonempty SMTP environment rejected | fixed local `smtp-test:1025` sink, actual SMTP protocol |
| AV | files blocked | mandatory ClamAV and original sandbox pipelines |
| Signature supply | disabled/files blocked | explicit operator-verified offline supply |
| Real PII | strictly false | strictly false |

Both schemas reject missing/extra/duplicate fields, wrong JSON types and unknown
combinations. Format 2 is a separate closed schema, not feature-flag opt-in.
`scripts/release.py` contains the exact supported packets (`MODES`, `FULL_MODES`).
The original reduced runtime catalog option and all disabled routes remain.

Full runtime requires **all** of: APP_ENV=staging, a controller-mounted read-only
authority file owned by a different UID, matching namespace/profile/modes digest,
fixed local DB host/name and the owner-applied synthetic database marker. APP_ENV
or modes JSON alone cannot authorize it. The authority is created only after
private-root, namespace, secret, TLS and exact-source checks. Every application
UoW rechecks the actual marker; direct staff/KB/privacy CLI and worker paths use
the same UoW. File factories and SMTP also check it. Marker checks use a separate
closed transaction so chat's REPEATABLE READ remains the first domain statement.
Only connection pools are reused; no marker, role or ACL decision is cached.

The existing owner-only `akbrizcase:synthetic-seed:demo` DB comment is reused for
compatibility with `app.seed_demo_consent`; the running environment stays staging.
The marker alone never authorizes full staging. The controller may apply it only
to a newly created owned DB volume after migration and empty order/staff checks.
An existing unmarked/foreign DB is BLOCKED, never automatically relabelled.

## Topology and resource envelope

The minimal extension is `compose.release.yml` plus the controller-selected
`compose.release-full.yml`. No development/production Compose file participates.
The small overlay preserves the original reduced topology without maintaining
two copies of its secrets, migration and TLS configuration. Operator `signed-cvd`
and disposable `synthetic-fixture` AV compositions are explicit in staging.json;
they are bound to the namespace claim and acceptance receipt.

| Service | Networks | Persistent data / published ports |
| --- | --- | --- |
| Nginx | ingress, internal web | TLS only; `127.0.0.1:PORT:8443` |
| API | internal web, data, mail | owned files volume; no ports |
| Worker | internal data, mail | same files volume; no ports |
| PG16 | internal data | owned database volume; no ports |
| SMTP sink | internal mail only | memory-only mail; no secrets/files/DB/ports/forwarding |
| Migrator/probe | internal tool networks | separate migrator/runtime credentials; no ports |

No external volumes, remote Docker daemon, socket mounts or production endpoints.
The controller forces the local Unix socket and strips ambient DB/SMTP/Compose
overrides. The full runtime also clears inherited outbound HTTP proxy settings.
Nginx has neither data/mail access nor application files or DB credentials.
The existing private `files` and `database` volumes are retained through failures.
Containers are read-only with bounded writable mounts, cap_drop and
no-new-privileges; full PG adds only its private runtime socket/tmpfs.
Actual mounts, ports, networks, ownership labels and hardening are inspected.

Initial bounds are intentionally retained pending actual full-rehearsal evidence:
API/worker/probe 1 CPU, 512 MiB, 64 PIDs; PG16 1 CPU, 768 MiB, 128 PIDs;
Nginx 0.5 CPU, 128 MiB, 32 PIDs. Sink: 0.25 CPU, 64 MiB, 32 PIDs.
Full rehearsal overlaps three real scans with three Argon2 hash/verify operations
in the bounded API container, runs three Uvicorn processes and linked browser
operations, and emits sampled memory/PID readings. This is synthetic-signature
capacity evidence only. A preliminary full run observed three API processes,
zero OOM kills and a cgroup memory peak at the 512 MiB hard cap. This shows memory
pressure and does not establish spare capacity; final-HEAD samples/counters are
reported separately. Limits are not raised to mask a fixture failure.
Large signed CVD memory/CPU requirements must be measured
separately on the selected closed host; no runner result certifies that capacity.
An OOM or insufficient resource envelope blocks acceptance, never skips scanning.

PG16 max_connections stays 100, including three superuser-reserved slots.
The original conservative PERF-001 ordinary budget is 74: API 3×8+0=24,
worker/operator-probe/maintenance/backup/migrator each 5+5=10. Full release adds
at most one guard connection per process: three API, worker and four conservatively
simultaneous tool classes = eight, total 82, leaving **15 ordinary slots**.
The readiness probe reuses its main class budget; tools normally run sequentially.
No extra API replica or parallel operator processes are permitted in this envelope.
Guard pools have size 1, overflow 0 and the existing bounded checkout timeout.

## Preparing a fresh closed staging

Use a clean checkout of the independently reviewed exact SHA on an isolated local
Docker host. Do not connect real infrastructure or run these against a production
namespace. The root must not exist, must be outside Git, caller-owned and 0700.
Root paths, secret files, passwords and CA/private key are never CI artifacts.

```sh
make release-test
make release-rehearsal RELEASE_REHEARSAL_ARGS='--profile full-synthetic-staging --prepare-only --root /tmp/akbriz-release-full-review'
python3 scripts/release.py check --root /tmp/akbriz-release-full-review --manifest /tmp/akbriz-release-full-review/first.json
python3 scripts/release.py apply --root /tmp/akbriz-release-full-review --manifest /tmp/akbriz-release-full-review/first.json
```

Preparation generates six independent random runtime secrets and a three-day
localhost certificate, verifies TLS chain/hostname/expiry/key matching, builds
exact-HEAD immutable backend/frontend candidates and resolves an actual PG16
registry digest. Secrets are read-only under the private parent, never argv,
Git or build arguments. Protect custody separately; no public-domain TLS or
production secret-custody acceptance is claimed. Check uses no migrations/seeds.

Fresh synthetic catalog and consent can then be prepared explicitly through the
existing seed commands. The following operator pattern retains the validated
Compose environment and a real terminal for protected CLI input:

```python
import subprocess, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from release import Release, read_json
root = Path('/tmp/akbriz-release-full-review')
r = Release(root, read_json(root / 'first.json'))
r.preflight()
for module in ('app.modules.catalog.seed', 'app.modules.orders.seed'):
    r.compose('run', '--rm', '--no-deps', 'api', 'python', '-m', module,
              '--mode', 'full-synthetic-staging')
subprocess.run(r.compose_command('exec', 'api', 'python', '-m', 'app.core.cli',
                                'staff-bootstrap'), env=r.env, check=True)
```

Use only synthetic logins/passwords and `.invalid` contacts. Existing hidden-input
`staff-create --role manager`, KB import/check/review/publish and tree commands
follow [MVP-043](MVP-043-operator-runbook.md). UI images retain MIME, quarantine,
double AV, re-encoding and current publication checks. Synthetic expert packets
are labelled SYNTHETIC ONLY; they do not grant real content/legal approval.
Full seed refuses an already populated fixture; API startup never seeds.

For a complete automated acceptance on a separate fresh namespace:

```sh
make release-rehearsal
make release-rehearsal RELEASE_REHEARSAL_ARGS='--profile full-synthetic-staging'
```

Each run stops its containers and retains volumes/private material even on failure.
The linked drill intentionally revokes documents and disables an account; use a
fresh preparation for a human walkthrough. Automated fixture staff passwords stay
in process memory and are not emitted. No real users can be admitted by this tool.

## SMTP, AV and privacy boundaries

Full SMTP uses the existing outbox/claim/lease/worker/retry/sender with a dedicated
`staging-sink` transport. It fixes host, port, sender `notify@example.invalid`,
recipient `manager@example.invalid`, no credentials and the exact trusted local
HTTPS admin origin. Every SMTP_* override fails closed. The original test transport
still requires APP_ENV=test; ordinary TLS/STARTTLS settings are not relaxed.
The existing closed sink receives actual DATA and cannot relay. Acceptance checks
451 failures, five attempts, protected manager retry, successful recovery, stable
Message-ID and exact payload-free message fields without duplicate business rows.
SMTP is at-least-once; this does not claim exactly-once mail or external delivery.

The synthetic AV composition is also explicit for a separate protected KB CLI
process: use `python /opt/release/browser_media_server.py --release-kb` followed
by the existing `app.modules.kb.cli` arguments inside the claimed API container.
The wrapper exists only in the verified test bind, requires full authority and
the DB marker, and preserves hidden terminal input, roles, transactions and real
ClamAV execution. Rehearsal reuses MVP-043's real PTY driver for admin preview and
manager denial, with no echoed credentials. Ordinary `python -m app.modules.kb.cli`
continues to require signed CVD; use it unchanged with the `signed-cvd` composition.

Operator AV (`av: signed-cvd`) requires an immutable 0555 signatures directory in
the private root with exactly main.cvd/daily.cvd/bytecode.cvd, regular 0444 files.
The unmodified production scanner checks sigtool signatures, freshness, bounds,
sealed snapshots and pre/post-scan database identity. Supply verified official
CVD through the operator's trusted offline channel **before** first check/apply.
This task does not fetch signatures or certify that external gate.

Disposable rehearsal additionally selects `compose.release-fixture.yml` and the
existing exact-source browser media composition. Only this trusted test bind
selects the synthetic NDB. Its fixed bytes/read-only mount/freshness are checked;
clean/infected checks still execute `/usr/bin/clamscan` and the real sandbox.
It does not enter runtime images or the production scanner. Missing/corrupted AV
dependencies fail readiness; arbitrary NDBs are never signed-CVD evidence.

MVP-037/038, AQ-01, erasure/replay protection, retention and publication rules stay
in force. The separate full-only marker/authority contract permits synthetic
consent collection, not real PII or legal approval. Existing linked cleanup,
withdrawal/history and denied internal-file scenarios are reused.

## Controller stages, upgrade and recovery

Sequence: PREFLIGHT → IMAGES → DATABASE → QUIESCE → MIGRATION (separate migrator)
→ SYNTHETIC_DATABASE → DEPENDENCIES → READINESS (least-privilege runtime role)
→ START → HTTPS SMOKE_TLS/MODES/FILES/BOUNDARIES → durable acceptance.
Only successful selected-profile HTTPS smoke produces accepted.json/status.json.
Image content IDs/digests, exact OCI revision, profile-support labels and schema
constant are checked. Bind-mounted infrastructure/test sources match that commit.

New format-2 receipts bind namespace, AV composition, profile, canonical modes
fingerprint and complete immutable source/image/schema manifest. Old two-field
reduced receipts retain their original interpretation. Same images do not grant
acceptance to another profile/namespace. Unknown/changed receipts are BLOCKED.

```sh
python3 scripts/release.py apply --root /tmp/akbriz-release-full-review --manifest /tmp/akbriz-release-full-review/candidate.json
python3 scripts/release.py rollback --root /tmp/akbriz-release-full-review --manifest /tmp/akbriz-release-full-review/first.json
```

Rollback requires a previously accepted candidate in this namespace/profile and
the exact compatible schema; it never migrates, downgrades, stamps or resets.
Rehearsal replaces two immutable packaging candidates of the same source/schema,
then verifies all retained business rows, immutable outbox identities and actual
document/image file bytes. This is not evidence for a future cross-schema rollback.

Failures print only RELEASE_BLOCKED_<stage> (migration authentication additionally
has a closed RELEASE_MIGRATION_* diagnosis). A post-quiescence failure stops
Nginx/API/worker, preserves DB/files and records blocked; no automatic fallback.
Keep ingress stopped, inspect the safe stage and private operator evidence,
restore the correct dependency/secret/marker only after establishing ownership,
then explicitly check/apply or compatible rollback. Never delete volumes, rewrite
schema metadata or forge acceptance to recover. Power loss remains in-progress.

## Target-load integration and remaining gates

Current [MVP-048 target](MVP-048-load-operational-smoke.md) is deliberately
incompatible: it requires APP_ENV=demo, `akbrizcase-https-test-*`, app_files volume
labels, the stock HTTPS proxy bytes, exact source-tree OCI identities and generator,
an independently bound dataset, empty transactional/quota tables and stopped
worker. Release uses staging, a different namespace/volume/proxy contract, an
internal mail network, release identity labels and an exercised nonempty DB.
Changing APP_ENV, relabelling volumes or erasing rows is not an integration path.

Next integration needs a separately reviewed target adapter version for this
topology/profile/authority, exact context/immutable identities, a **fresh** empty
synthetic dataset with independent digest approval, trusted TLS, host/project and
operator identity, backup/runtime evidence and operator-quiesced worker. Preserve
all existing authorization and pilot200-v1 thresholds. Obtain separate explicit
authorization before any target load; RELEASE-001 never runs it.

[MVP-042](MVP-042-backup-restore.md) remains the encrypted clean-restore/journal
contract; in-place release rollback is not backup. [MVP-043](MVP-043-handover-checklist.md)
still needs independent human acceptance. [MVP-050](MVP-050-go-no-go.md) still needs
real infrastructure, signed signature supply, SMTP, RF offsite, custody, legal,
content/expert/rights and operator evidence. The known PERF-001 diagnostic p95
370.070 ms versus 300 ms remains FAIL; this task changes no performance threshold.
The exact final CI run and its actual MVP048_RESULT/v2 belong in the PR report;
green functional smoke never implies target or performance PASS.

FULL_MVP_READINESS=BLOCKED  
REDUCED_PILOT_READINESS=BLOCKED  
TARGET_LOAD=BLOCKED  
PUBLIC_LAUNCH_RECOMMENDATION=NO-GO  
HUMAN_GO_NO_GO_DECISION=PENDING  
PRODUCTION_ADMISSION=BLOCKED
