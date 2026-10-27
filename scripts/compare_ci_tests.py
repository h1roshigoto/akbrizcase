"""Compare executed test identities/outcomes in downloaded GitHub job logs.

Usage: python3 scripts/compare_ci_tests.py BEFORE_DIR AFTER_DIR
Each directory contains backend.log, frontend.log, database.log and compose.log.
Only test IDs/outcomes and static smoke checkpoints enter the JSON report.
"""

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
TIMESTAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ")
RANDOM_UUID_CASES = (
    "tests/test_applicability_evidence.py::test_review_rejects_missing_or_fabricated_permission[actor_id-",
    "tests/test_kb_lifecycle_contracts.py::test_review_requires_operator_evidence[reviewed_by-",
    "tests/test_orders_http.py::test_invalid_input[consent_id-",
)
UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


def normalize_id(identifier):
    # These existing parameter values use uuid4() at collection time. Preserve
    # every other parameter/ID, including UUIDs outside these exact cases.
    for prefix in RANDOM_UUID_CASES:
        if re.fullmatch(re.escape(prefix) + UUID4 + r"\]", identifier):
            return prefix + "<uuid4>]"
    return identifier


def inventory(directory):
    result = {}
    for job in ("backend", "frontend", "database", "compose"):
        entries = []
        collected = []
        log = (directory / (job + ".log")).read_text()
        for line in log.splitlines():
            line = TIMESTAMP.sub("", ANSI.sub("", line)).strip()
            if match := re.fullmatch(
                r"(tests/.+?::.+?)\s+(PASSED|SKIPPED|FAILED|ERROR)(?:\s+\[.*\])?", line
            ):
                entries.append((normalize_id(match[1]), match[2]))
            elif match := re.fullmatch(
                r"(PASSED|SKIPPED|FAILED|ERROR) (tests/.+?::.+)", line
            ):
                entries.append((normalize_id(match[2]), match[1]))
            elif match := re.fullmatch(r"✓ (src/.+? > .+?)\s+[\d.]+\s*m?s", line):
                entries.append((match[1], "PASSED"))
            elif line.startswith("PASS: "):
                entries.append(
                    (re.sub(r"HEAD=[0-9a-f]{40}", "HEAD=<sha>", line), "PASSED")
                )
            elif line.startswith("tests/") and "::" in line:
                collected.append(normalize_id(line))
        if job == "backend" and not entries and collected:
            # The original command stays unchanged. Its all-passed summary,
            # followed by collection in the same checkout, proves execution of
            # the complete ID set. Refuse missing results or any skip/failure.
            counts = [int(n) for n in re.findall(r"\b(\d+) passed(?:,| in)", log)]
            if counts != [len(collected)] or re.search(
                r"\b\d+ (?:skipped|failed|error)", log
            ):
                raise ValueError(
                    "Backend execution does not match its collected inventory"
                )
            entries = [(identifier, "PASSED") for identifier in collected]
        if not entries:
            raise ValueError("No executed identities found for " + job)
        result[job] = Counter(entries)
    return result


def compare(before, after):
    report = {}
    for job, expected in before.items():
        actual = after[job]
        canonical = json.dumps(sorted(expected.items()), ensure_ascii=False)
        report[job] = {
            "executions_before": expected.total(),
            "executions_after": actual.total(),
            "baseline_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "missing_or_changed": sorted((expected - actual).items()),
            "added_or_changed": sorted((actual - expected).items()),
        }
    return report


if __name__ == "__main__":
    before, after = (inventory(Path(arg)) for arg in sys.argv[1:])
    report = compare(before, after)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(
        any(r["missing_or_changed"] or r["added_or_changed"] for r in report.values())
    )
