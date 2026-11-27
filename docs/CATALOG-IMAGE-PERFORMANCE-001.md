# CATALOG-IMAGE-PERFORMANCE-001

High priority for commercial launch. [Issue #94](https://github.com/h1roshigoto/akbrizcase/issues/94) · [Historical image failures #74](https://github.com/h1roshigoto/akbrizcase/issues/74).

## Measured baseline

Source `cd49d2ad5ab450f8608c01862470ef4026d2c06f`. The isolated test used ten distinct synthetic 1280 × 960 PNGs, trusted HTTPS through Nginx, PostgreSQL 16, three API workers, and the #84 API limits of 512 MiB, 1 CPU and 64 PIDs. Each image passed through the existing upload/verification functions to create a real fixture and receipt. The signed run used a read-only copy of the operator's authentic CVD set for reads. Timers wrapped existing calls in the test launcher and logged only durations, counts and status. First-request samples are first image requests on a new stack. No host page caches were dropped.

### HTTPS and phases

| Database for scanning | Requests | HTTP | TTFB | Batch elapsed |
| --- | ---: | --- | ---: | ---: |
| Test signature | First 1 | 200 × 1 | 485 ms | 486 ms |
| Test signature | Repeat 1 | 200 × 1 | 354 ms | 355 ms |
| Test signature | 2 | 200 × 2 | 665–666 ms | 667 ms |
| Test signature | 6 | 200 × 6 | 778–2,525 ms | 2,527 ms |
| Test signature | First 10 | 200 × 10 | 860–4,158 ms | 4,161 ms |
| Test signature | Repeat 10 | 200 × 10 | 787–4,213 ms | 4,216 ms |
| Signed CVD, #84 limits | First 1 | 404 × 1 | 8,685 ms | 8,687 ms |

The two ten-request runs have sample p95 TTFB of 4.16 s and 4.21 s (nearest rank, ten samples each). The single warm request is a point observation. Response body delivery took 0.04–0.23 ms. A warm single request took about 350 ms in the backend and 354 ms at the HTTPS client. The catalog-list TTFB was 31 ms with the test signature and 27 ms with signed CVD. Nginx forwards the request to the API without proxy caching. The application timers explain the observed delay. [Release Nginx configuration](../infrastructure/release/nginx.conf).

| Backend phase | Test signature across 30 requests, median / max | Signed first request |
| --- | ---: | ---: |
| `FileAdmission` wait | 841 / 3,403 ms | 0.10 ms |
| File and receipt reads, format check and SHA‑256 | 0.35 / 1.10 ms | 0.13 ms |
| Sealed snapshot creation | 0.05 / 1.04 ms | 0.05 ms |
| CVD verification via `sigtool` | synthetic fixture | 1,970 ms before scanner failure |
| `clamscan` (engine load plus scan) | 259 / 378 ms | 6,536 ms before scanner failure |
| Pillow isolated parser and complete decode | 510 / 793 ms | not reached |
| PostgreSQL UoWs | 27 / 104 ms | 5.59 ms, initial check |

Zero AV calls were observed inside a catalog read UoW. The successful route checks publication and the current image key before admission and after the file work. It also checks the current bytes/receipt before returning the sealed snapshot. The synthetic API peaked at 372,838,400 bytes and 29 PIDs, with zero OOM, memory-limit or PID-limit events. The signed API hit 536,870,912 bytes, recorded one OOM kill and reached a swap peak of 536,870,912 bytes. The log recorded `SCANNER_FAILURE`, and `/readyz` still returned 200 afterwards. Testing stopped at the first signed failure to avoid repeating an established OOM. Signed warm and signed ten-request HTTPS acceptance remain unverified. [Read endpoint](../backend/app/modules/catalog/router.py) · [Image pipeline](../backend/app/modules/catalog/images/pipeline.py).

### Isolated AV component measurements

Component probes used the project's real scanner and sandbox commands, one CPU, no network, and sealed in-memory synthetic images. The standalone 1,792 MiB cap is for measurement only. The API rehearsal cap stayed at 512 MiB.

| Case | `sigtool` | `clamscan` | Decode | Total | Cgroup peak |
| --- | ---: | ---: | ---: | ---: | ---: |
| Test signature, first | fixture | 141 ms | 266 ms | 408 ms | 181 MB |
| Test signature, repeat | fixture | 95 ms | 218 ms | 313 ms | 181 MB |
| Signed, 512 MiB | 1.29 s | 2.09 s then OOM | — | 3.38 s | 512 MiB |
| Signed, 1,792 MiB, first | 2.53 s | 6.56 s | 212 ms | 9.30 s | 1,176,637,440 B |
| Signed, 1,792 MiB, repeat | 2.58 s | 6.43 s | 218 ms | 9.23 s | 1,180,426,240 B |
| Signed, 1,792 MiB, 2 × 2 pixels | 2.51 s | 6.39 s | 213 ms | 9.11 s | 1,180,516,352 B |

Each successful signed GET verifies three signed archives before scanning and all three again afterwards. `clamscan` constructs a new engine for each invocation. Almost the same scan duration on a 2 × 2 image and a 1280 × 960 image, plus the resident-engine results below, identify startup and database loading as the dominant cost. The synthetic profile still launches scanner and decoder processes per image, and one CPU feeds a two-slot queue, so a page can be slow even without full CVDs. The supplied real-site measurements are of the same order as standalone signed scans. The real site itself was outside the test environment. [Scanner implementation](../backend/app/modules/kb/files/scanner.py) · [ClamAV scanning documentation](https://docs.clamav.net/manual/Usage/Scanning.html).

## Candidate architecture for approval

### Resident scanner with a fresh scan per read

A local component experiment used ClamAV 1.4.6, authentic CVD, one CPU, one scanner thread, a bounded queue, no TCP or network, an isolated Unix socket, sealed descriptors via `FILDES`, and disabled result caching. The verified generation took 1.42 s for `sigtool`, then 7.55 s to load and answer `PING`. One warm scan took 17 ms; ten warm scans completed in 246 ms total, including fixture construction and queuing. Their individual socket latencies were 101–178 ms. The ten-request batch used about 252 ms CPU; the cgroup peaked at 1,115,844,608 bytes and 15 PIDs with no OOM, memory-limit or PID-limit events. After shutdown the socket refused requests. Restart took 7.19 s and the next scan passed. These are scanner-only timings. [ClamD Unix socket protocol](https://docs.clamav.net/manual/Usage/ClamdProtocol.html).

The approved local prototype then connected the same approach to the actual catalog pipeline on a fresh isolated HTTPS stack. Only the trusted test launcher substitutes the scanner methods. It starts a separate, networkless `clamd` container with a read-only copy of authentic CVDs, a separate UID, a private Unix socket, a pinned startup generation and sealed-descriptor scans. It verifies signatures and file digests before activation, compares the loaded daily version, and checks that the marker stays fresh and unchanged across the scan. The code and limits of the API file/decode and DB paths stay the same.

| Signed CVD through the prototype | HTTP | TTFB | Batch elapsed |
| --- | ---: | ---: | ---: |
| First 1 after ready | 200 × 1 | 245 ms | 246 ms |
| Warm serial, 12 reads | 200 × 12 | 240–404 ms, sample p95 404 ms | serial run |
| Warm 6 | 200 × 6 | 489–1,478 ms | 1.48 s |
| Warm 10, first round | 200 × 10 | 512–2,638 ms | 2.64 s |
| Warm 10, second round | 200 × 10 | 542–2,577 ms | 2.58 s |

Warm serial p95 and both ten-image makespans met the proposed targets in this test. At ten simultaneous requests, p95 TTFB was 2.58–2.64 s. The two admission slots and full decoder are now the main burst costs. Across 45 requests, the scanner span median was 15.48 ms, decoder median was 432.77 ms and the longest admission wait was 2.18 s. Zero scanner calls overlapped a request's DB UoW. The API peaked at 434,417,664 bytes and 30 PIDs under the existing #84 limits. The separate scanner peaked at 1,063,297,024 bytes and 11 PIDs under diagnostic limits of 1,792 MiB, one CPU and 32 PIDs. Neither cgroup recorded a cap event, OOM, PID-limit event or swap. The verified sidecar boot took 9.40 s. An expired test marker and a stopped sidecar both returned 404 without image bytes; restarting the verified scanner took 9.52 s and restored HTTP 200.

This protocol test used a fixed CVD generation. ClamAV 1.4.6 on the baseline rejects the `EnableReloadCommand` configuration directive. The prototype therefore exposes the socket only inside the owned experiment and disables its periodic self-check. A production design must pin a version with suitable command controls or restrict the protocol through a narrow local broker, then test actual generation rotation. No resident-scanner integration is included in the runtime diff.

1. Keep the existing server-side fresh scan and full decoder on every GET. Give the scanner a separate cgroup with a measured, explicitly approved budget. The current API cap stays at 512 MiB. Keep the API's two-slot admission and bound scanner threads, requests, message lengths and timeouts. The measured signed engine alone exceeds 512 MiB. Provisioning and failure/reload budgets need confirmation; ClamAV documents substantially higher transient usage for concurrent reload. [ClamAV Docker sizing](https://docs.clamav.net/manual/Installing/Docker.html).
2. Make the Unix socket private to the API/scanner group. Run non-root with a read-only root, distinct UID, no capabilities or network, a bounded tmpfs and no app volumes, DB credentials or product paths. Pass the sealed file descriptor rather than a filename from a shared application volume. Disable client reload/shutdown commands, limit any implicitly available operations to this private boundary, and keep signature updates under the operator's existing verified offline procedure. Only accept the expected complete verdict; disconnects, malformed replies, timeouts, infected files and stale signatures fail closed.
3. Verify each immutable signature generation cryptographically before activation and bind the ready instance to the three CVD digests, engine identity, daily freshness and policy identity. Treat `VERSION` alone as insufficient. Check the active generation/freshness on each request and verify that the generation still matches before returning the result. For rotation, stop admitting the old generation, drain or invalidate in-flight scans, and admit only after the new generation is verified and ready. Start with a controlled stop/start to bound peak memory during reload. Re-evaluate concurrent reload only after measuring both engines together.
4. Retain both short DB checks, the sealed snapshot, the current-image recheck and existing read limits. Exercise changed bytes/receipt, revoked publication, replaced keys, invalid/expired/updated signatures, socket failure, rotation races, restart and overload before comparing end-to-end warm p95 and ten-image makespan. A 500 ms p95 and a three-second batch are targets to verify, especially given the measured decoder CPU and queueing.

### Reusing a verification result

A separate policy decision would be needed to reuse a verdict. The minimum candidate key includes the SHA‑256 of the exact sealed bytes, verified digest of each CVD plus freshness, ClamAV engine/build identity and effective limits/options, and the decode/policy version. Only complete verified results on immutable bytes are eligible. Rehash current bytes, perform both DB checks and fail closed on ambiguity. Bound cache size, expiry and concurrent misses. Revoke the generation on change, rollback, expiry or AV failure, including races during a cache hit. Never grant visibility based on the result, a URL, a receipt or a previous `clean` flag. Measure this separately if fresh resident AV plus decode still misses the targets.

### HTTP and browser

The agreed temporary overload contract for a still-current published image after admission expiry is HTTP `503 Service Unavailable`, the existing sanitized `SERVICE_UNAVAILABLE` envelope, `Retry-After: 5`, and `Cache-Control: no-store`. The API rechecks publication and the key after expiry, so a retired/replaced item still receives the normal 404. Missing, corrupt or infected content retains the established fail-closed response. The retry interval is advisory. [HTTP 503 and Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html).

The baseline fault was reproduced through HTTPS on an isolated synthetic stack. Another execution held both real `FileAdmission` slots. An otherwise readable image waited 20,000 ms for admission and returned HTTP 404 `NOT_FOUND` at 20,014 ms TTFB. Its backend event was `PIPELINE_BUSY`. Releasing the slots restored HTTP 200 on the same image. The image was not scanned during the expired request. The #74 observation and this reproduction are separate experiments.

Using the same held-slot HTTPS reproduction after the patch returned HTTP 503 `SERVICE_UNAVAILABLE` after 20,020 ms with `Retry-After: 5` and `Cache-Control: no-store`. After release, the same request returned 200. The 20-second admission window and retry semantics are independent of the experimental performance change.

`ItemImage` already sets native `loading="lazy"`. It displays the fallback after a failed request and does not retry the same URL during that component lifetime. Native image errors cannot expose the application response status and retry header to this handler. A bounded same-origin retry with jitter and cancellation, keyed to the current URL, can be considered alongside frontend ownership. Lazy scheduling reduces offscreen work and does not reduce processing cost when ten images are requested together. A future proxy cache or ETag change requires separate approval because current `no-store` is part of the publication contract. A conditional GET/304 would still need normal publication, current-key and accepted security checks before confirming reuse; using an ETag solely for revalidation saves the measured sub-millisecond body transfer. [ItemImage](../frontend/src/components/ItemImage.tsx) · [HTTP caching](https://www.rfc-editor.org/rfc/rfc9111.html).

In one Chromium run on the synthetic HTTPS stack at a 1024 × 720 viewport, all ten catalog images had native lazy loading and Chromium started all ten before any test scroll. All ten returned 200. Browser resource timing reported about 1.03–3.18 s from request start to headers and 0.3–0.8 ms for the body. Native lazy loading alone did not limit this catalog to a small request burst. The browser was scoped to the synthetic server certificate for this run.

## Baseline verification and open acceptance

- The baseline passed 127 focused backend tests with real local ClamAV/sandbox and synthetic signatures. PostgreSQL 16 image suites passed 63 tests after the patch. The new cases cover actual admission expiry and recovery, and archive or replacement committed while a requester waits. The existing suite covers access and binding, other replacement and publication races, file mutation and failures.
- With authentic CVDs, the baseline verifier accepted a current database, rejected the same signed generation at a controlled time past expiry, and rejected a deliberately corrupted copy in the isolated container. Two authentic generations were unavailable for a rotation exercise. The standalone daemon exercise checked socket loss and restart; it did not exercise application fail-closed behavior against a resident daemon.
- The signed prototype met the serial and batch targets in the measured runs. Production approval still requires an integrated scanner implementation, tests for two valid signature generations, restart and failure handling under production wiring, and repetition on the target hardware. Product traffic still uses the fresh `clamscan` path.
