# MVP-039 — HTTP protection and transactional quotas

Status: Draft; independent review and exact-HEAD CI evidence required. Synthetic
use only. This task does not approve or close any MVP-037/038 release gate,
change privacy cleanup, deploy, merge or begin MVP-040.

## Counters and transaction semantics

PostgreSQL 16 owns `rate_limit_buckets` (additive revision
`0011_mvp039_http_security`, after `0010_mvp038_journal_basis`). No existing data
or privileges on other objects change. Runtime CRUD on this new ephemeral table
supports the API counters and the existing worker's independent expiry tick.

| Operation | Key | Limit / fixed window | Charged operations |
|---|---|---|---|
| New chat session | domain-separated HMAC of socket/trusted IP | 10 / 3600 s | Committed session creation |
| New order | domain-separated HMAC of socket/trusted IP | 5 / 3600 s | Committed new order and its atomic write set |
| New chat exchange | SHA-256 of session UUID | 30 / 60 s | Committed new exchange |

The first accepted counter acquisition sets `started_at` from PostgreSQL
`clock_timestamp()` and a fixed expiration; subsequent successes do not extend
it. A window is inactive at `expires_at <= now`. The clock is read **after**
transaction-scoped advisory locks. Sorted locks and UPSERT synchronize every
retained rotation alias across independent pools/processes. The count and the
domain writes commit together; exceptions, UNIQUE losers and late constraint
failures roll both back. Invalid requests, state conflicts and authenticated
replays cost nothing. Message ownership/expiry and request identity precede the
quota. No in-memory counters or Redis.

A non-locking new-order quota precheck avoids unnecessary creation work when the
quota is already full. It grants no reservation: the authoritative locked check
runs after the whole creation write set, before commit. PostgreSQL UNIQUE still
arbitrates same-key races; the loser rolls back and reads the winner in a new UoW.
Replay still precedes current catalog/consent validation.

## Full order quota and AQ-01 clarification

The owner delegated the exact full-quota behavior in the MVP-039 conversation.
When quota is available, the accepted AQ-01 matrix remains unchanged. When it is
full:

| Input with valid body transport | Result |
|---|---|
| Eligible retained key + authenticated original token + identical body | 200 original receipt |
| Eligible retained key + authenticated original token + changed body | 409 IDEMPOTENCY_CONFLICT |
| Unused key | 429 RATE_LIMITED |
| Hidden key: wrong token, erased record, or expired replay | Same 429 RATE_LIMITED |
| Missing/malformed required headers or malformed body transport | Normal 422 |

Returning 404 for a hidden key but 429 for an unused key would disclose key
occupancy to an exhausted caller. The shared 429 has no object/quota identity,
remaining-count, HMAC, token, or IP details. After capacity is available, hidden
keys return AQ-01's generic 404 again. Correct-token conflicts do not test or
consume quota. No new post-erasure verification material is stored. This is an
explicit saturation rule, not a change to erasure or 24-hour replay eligibility.

Backend Retry-After is `max(1, ceil(expires_at - PostgreSQL now))`; errors use the
safe envelope, a server request UUID and no-store. A later successful concurrent
operation can consume newly available capacity, so Retry-After is not a reserved
slot. Response equality does not establish constant-time key lookup: AQ-01's
existing timing limitations still apply.

## Nginx and transport trust

The shipped HTTP and HTTPS configurations share `api_limit` at **120r/m** and
`login_limit` at **5r/m**. These are Nginx **leaky buckets**, respectively with
`burst=119 nodelay` and `burst=4 nodelay`: an idle client can immediately send
120 API requests / 5 login attempts, then capacity returns every 0.5 / 12 s.
This is not a calendar-minute window or an in-flight concurrency limit. Login
attempts count even if authentication, Origin or CSRF subsequently fails. HTTPS
`/admin/auth/login` uses both zones. HTTP staff paths remain closed. API limiting
covers `/api/`; HTTPS administrative paths also use the general zone. No public
headers select the Nginx key: it is always `$binary_remote_addr`.

Nginx 429 responses use a fixed safe JSON envelope and formatted random request
UUID, no-store, Retry-After 1 s (general) / 12 s (login). Normal access logging is
off; routine request-bearing error messages are suppressed. This task adds no
MVP-040 logging/metrics subsystem.

Uvicorn remains `--no-proxy-headers --no-access-log`. Compose publishes only
Nginx on host loopback; API/PostgreSQL have no published ports. API and Nginx
share the private `web` network; Nginx has no `data` attachment and no app files,
DB data or runtime-secret mounts. Container/network administration belongs to
the deployment trust boundary.

Only the exact transport peer resolved for the deployment-owned Docker DNS name
`nginx` may assert `X-Akbriz-Client-IP` and `X-Akbriz-Scheme`. Resolution is
refreshed on use, handling startup ordering/replacement without trusting a stale
released address. No broad private CIDR is trusted. DNS failure grants no proxy
trust: the actual socket address remains the identity (potentially grouping
clients and denying capacity). Malformed/duplicate trusted assertions fail
closed. Direct backend callers use their own socket IP; arbitrary XFF, Forwarded,
X-Real-IP and dedicated-header claims from other peers are ignored.

Every Nginx proxy location overwrites dedicated assertions and Host (including
its port) and removes XFF, Forwarded, X-Real-IP and X-Forwarded-Proto. The dedicated
scheme affects same-origin validation only; it does not activate Uvicorn proxy
header handling. Staff retains its explicit HTTPS Host/Origin allowlist and
existing server-session CSRF verification.

## Secrets, rotation and expiry

Secrets are runtime files only. `make init-secrets` creates a random independent
`ip_hmac_secret`; Compose mounts it only into API. Host check-secrets requires
private files or read-only files in a private 0700 parent. No inline secret env
fallback is accepted. Source/image contexts contain no runtime secret files;
BuildKit CA mounts used locally contain trust roots, not application credentials.
No raw IP, token, canonical customer body or secret goes into a bucket.

Both HMAC loaders accept the legacy single-key file or a bounded JSON ring:
`{"active":"<random active key>","previous":["<previous key>"]}`. Values here
are placeholders. Generate at least 256 random bits per key, keep keys unique,
validate protection, and atomically replace the runtime file. Restart API
processes to load the new ring; do not print/dump files, process environments,
Compose config, image environment secrets or credentials into CI artifacts.

Order rotation: keep every previous key for at least **24 hours after the last
old-key writer stops**, covering the full eligible original replay window.
Deploy the new active + all overlapping previous keys to every process before
retiring an old key. At most four keys are accepted: rotation cadence must fit
this bound. Never drop an eligible key to make a configuration fit. Token/body
hash erasure remains atomic and takes precedence over any rotation.

IP rotation: use the independent IP ring, carry old keys on every process during
rolling restart, and retain overlap for at least 24 hours after old writers stop.
Counters for all retained aliases are updated atomically using their maximum
current count/window, so rotation does not reset the quota. No disjoint rings
may run concurrently. HMAC input also includes an hourly epoch read from the
PostgreSQL clock: current and previous hours overlap, so an hour boundary cannot
reset an active quota, and no IP pseudonym is renewed across more than two hours.
Four secret keys produce at most eight aliases, with a shared current window.
This limits linkability even when a runtime secret stays active for longer.

Buckets live at most one hour (messages one minute), with a database lifetime
constraint and expiry index. The worker deletes up to 1000 expired rows every
normal 2-second tick, independently of SMTP/privacy approval/journal execution.
Provision enough worker capacity to drain expiry within 24 hours; verify the
oldest expired bucket operationally. If workers are stopped/backlogged, physical
removal is delayed: deployment must restore/drain expiry before the 24-hour IP
retention bound; no application can promise wall-clock deletion while all
execution is stopped. Persistent backups/host retention remain existing open
privacy/operational gates, not a new production approval.

## Browser policy and acceptance

Public API requests with Origin require exact external scheme + authority;
foreign/null/duplicate Origin and cross-site mutations are denied without CORS
permission. Non-browser requests without Origin remain possible with existing
JSON/header-token contracts. Administrative CSRF/Origin/session boundaries are
unchanged. Sensitive API/admin responses, including 429 and errors, are no-store.

Frontend CSP allows only same-origin scripts; API/admin CSP adds a fresh nonce
for the existing SQLAdmin CSRF bootstrap. No unsafe-inline/unsafe-eval scripts,
third-party scripts, objects, framing or foreign form actions. SQLAdmin assets
remain local; React/Jinja continue escaping untrusted text. Swagger/ReDoc's
third-party-script pages are disabled; OpenAPI JSON remains available.

`make integration` includes `http-security`: real PostgreSQL and two actual
Uvicorn child processes, concurrent IP creation limits, same-key last-slot race,
all relevant rollback/429/envelope cases, quota-saturation replay, spoofed headers,
30 messages across two processes, expiry and overlapping keys. Existing AQ-01
rotation/race/erasure regressions remain. The long diagnostic-transition test
explicitly advances its synthetic bucket window at exchange 30: it still checks
all 33 transitions rather than bypassing the configured production quota.

`make http-security-smoke` adds trusted HTTPS Chromium, HTTP+HTTPS proxy behavior,
CSP/HTML, CORS/Origin, quotas, free replay, login/API Nginx 429s, no-store and
private storage denial. Existing auth/CSRF and frontend escaping suites remain.
The stacked PR's CI trigger includes its explicit MVP-038 base, preserving all
four jobs and their exact-head checkout/inventory checks.
