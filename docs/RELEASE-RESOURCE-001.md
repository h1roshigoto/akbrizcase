# RELEASE-RESOURCE-001 — release test composition and memory evidence

Owner: Данил. [Issue #83](https://github.com/h1roshigoto/akbrizcase/issues/83).
Base: `7a1eb784aba44c7a4cfe6444543dc7fa3a82cba7`. The Draft PR records its exact
HEAD, measured candidate results, complete CI and independent review status.
This document describes the correction and reproduction, not an approval.

## Cause and smallest correction

The original release fixture imports `app.main.app` at module scope. The resource
driver imports `release_scanner` from that module and therefore initializes an
unused ASGI application. Running that same file as the three-worker launcher
also initializes the application in the supervisor and multiprocessing bootstrap.
Worker health/protected CLI/probe processes import it too. This includes module
initializers, not merely a reference to an already running worker's application.

An isolated import probe on unchanged main measured PSS 47,874,048 bytes after
the file pipeline import and 70,360,064 after the browser/scanner module import;
`app.main` was loaded. Moving only the app import into the factories measured
61,462,528 bytes and no `app.main` in the resource driver. That first candidate
(`62cf35388de5b1ab7eb3e3d2365c12958505ddd2`) passed its original six-client
workload, but still reached 536,870,912 bytes, `memory.events.max=657`, with
`oom_kill=0` and `pids.events.max=0`. It is explicitly **insufficient**.

The final composition also launches the importable document factory directly
through Uvicorn in `compose.release-fixture.yml`. The supervisor loads Uvicorn;
each of the same three workers creates the original application and enters its
full original lifespan. ASGI initialization remains inside the two factories.
The synthetic scanner context, real ClamAV, decoder, security checks and runtime
application sources are unchanged. No admission gate, queue, new service, API,
ACL, migration or authentication change is introduced. Queue/refusal semantics
for a new resource admission mechanism are consequently not claimed.

The existing source/immutable-image/profile/ownership/TLS guards remain in place.
The release controller already checks both changed fixture source files against
the exact manifest commit. The helper is executed as part of the exact-HEAD test
driver; it is not mounted in, or copied into, application images.

## Stronger acceptance evidence

The six original concurrent tasks remain: three file pipelines (one infected,
two safe), and three Argon2 hash/verify pairs behind `Barrier(6)` and a six-thread
pool. Assertions retain the three worker count and all safe/infected outcomes.
They additionally require the actual infected rejection code, original Argon2
parameters `(3, 65536, 4)`, overlap of all six operation intervals and completion
within the original 60 seconds. No reduced payload, sleep, skipped check or
retry-until-pass loop is introduced.

`release_resource_probe.py` collects numeric cgroup current/peak/limits/events,
CPU quota and PID bounds, optional swap/peak counters, and role-only RSS/PSS and
SwapPss. It records the roles observed near the sampled memory maximum and each
role's independent maxima. These are non-atomic samples: independent role maxima
must not be summed or described as a simultaneous allocation breakdown. Kernel
`memory.peak` and event counters are authoritative for cap/OOM evidence.
Process arguments select fixed roles internally and are never emitted.

Acceptance refuses any cap event, OOM event/kill, PID-limit event, widened memory,
CPU or PID limit, sampled swap use or supported nonzero swap peak. It checks the
sampler's termination and absence of leftover scanner/parser processes. Sampling
errors cannot certify acceptance. Failure output retains numeric kernel counters.
Missing mandatory Linux/cgroup evidence blocks acceptance; unsupported optional
peak counters remain explicitly null. The monitor adds a thread in the existing
driver, not another application process.

Tests cover the lazy import boundary and three-worker factory/lifespan contract,
valid evidence and sampler cleanup after an operation refusal, cap/OOM/PID/swap
refusals, widened bounds and numeric-only output even with private process args.
The existing full security, real scanner/parser, PG16 and linked HTTPS suites
remain mandatory, including image publication/binding/revocation and cleanup.

## Reproduction and actual investigation limits

Use only a clean exact-HEAD checkout and a fresh controller-owned synthetic root:

```sh
make release-test
make acceptance-guards
make backend-test backend-lint backend-types
make release-rehearsal
make release-rehearsal RELEASE_REHEARSAL_ARGS='--profile full-synthetic-staging'
```

The full run's `FULL_REHEARSAL_RUNTIME_RESOURCES` contains bounded numeric
measurements. Repeat the complete command on another fresh namespace, then check
the nine mandatory CI jobs on that exact HEAD. Do not reset cgroup counters,
reuse a failed cap-saturated run as proof of margin, or retry OOM until PASS.

Investigation also used the existing `--prepare-only --root` mode followed by
`scripts/release.py apply` and `files_and_resources(Release(...))`, with an external
role-only sampler for comparison. Private root values and raw child logs are not
published. The latter sampler adds one Python observer; its overhead must be
acknowledged when comparing it with the final in-driver monitor.

Local main preparation initially stopped because Docker's default address pools
were exhausted. Only new namespace-owned networks were precreated with free
subnets and the original topology/internal flags; existing networks/volumes and
daemon settings were untouched. An initial partial setup was retained and stopped;
the controller marked a different fresh DB through its normal ownership guard.
Subsequent unchanged-main runs stopped at START, including from a separate native
Linux checkout, before the resource workload. These are not local successful stress
reproductions. #83 retains the earlier exact-source CI OOM evidence and cap-saturated
main results; neither the individual kernel victim nor its allocation is invented.

Local Docker permits 512 MiB swap in addition to its 512 MiB memory cap. A local
zero-OOM result alone therefore proves no headroom. Later image preparation also
encountered registry authorization networking failures, before workload execution.
All actual candidate runs and any remaining blockers belong in the PR report;
unexecuted or failed checks must not be reported as successful.

The result is confined to the synthetic-signature rehearsal. It does not certify
official CVD capacity, production load, #74, or #80 acceptance. Stable, existing
DBs/volumes/credentials and Alexander's frontend #82 are outside this change.
Independent review and the human merge gates in TEAM-WORKFLOW remain required.
