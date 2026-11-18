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

The direct Uvicorn launcher (`fc1ad3992b3bc65b30601184bb441232d0d18987`)
still reached the cap in CI and locally. CI recorded 106 memory-limit events,
38,731,776 bytes of swap peak, and zero OOM or PID-limit events. The locally
reproduced failure recorded 197 memory-limit events and 30,810,112 bytes of
swap peak. An additional import-only experiment in the file driver and scanner
bootstrap still saturated the cap on its own.

The owned release fixture keeps Uvicorn's supervisor and uses Linux forkserver
worker creation. An isolated three-child import probe measured 222,478,336
bytes of aggregate cgroup current with spawn, 136,409,088 with route preload,
and 105,840,640 when preloading the dormant app itself. These import probes
did not run the lifespan or the release workload. Preloading only route
definitions still hit the cap in a full local rehearsal.

The forkserver now constructs a single dormant copy of the original ASGI app
and freezes preloaded objects for copy-on-write sharing. It checks that the
application has no attached database and only one thread before forking.
Each of the three workers composes its own application state, restores safe
runtime logging and enters the original lifespan, with independent database
connections. Uvicorn keeps ownership of listener sockets, readiness, worker
supervision and shutdown. The fixture and its preload module are verified
against the manifest commit before mounting. Production worker creation is
unchanged.

With the dormant app preloaded, a full local run still hit 512 MiB with 11
cap events and 2,052,096 bytes of swap peak. At a sample near saturation it
observed three sandbox bootstraps alongside the Argon2 operations. The shared
file bootstrap now loads PDF modules only for PDF tasks, still before applying
the pathname-denying seccomp filter. The ClamAV command, limits, isolation,
signature checks, and PDF/image validation semantics are unchanged.

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

Acceptance requires a kernel memory peak strictly below the limit. It refuses
any cap event, OOM event/kill, PID-limit event, changed memory/CPU/PID limit or
swap use. With swap allowed, the kernel swap peak must establish zero use.
The probe checks three workers near the sampled peak and at completion, sampler
termination and absence of leftover scanner/parser processes. The full
rehearsal checks cumulative API cgroup counters before replacement, after
upgrade and after both rollback paths. Failure output retains numeric kernel
counters. The concurrent workload's monitor uses a thread in the existing driver.

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

The Linked browser E2E job uses the official Ubuntu archive over HTTPS for
both Azure and EC2 runner mirrors. Two runs reached the existing 20-minute
deadline during the second browser stack after downloading 32.1 MB of
Chromium dependencies from the EC2 mirror in over five minutes. The job still
installs signed packages, uses pinned Chromium, runs both fresh stacks and
keeps its original timeout.

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
