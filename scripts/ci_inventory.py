"""Write test IDs/outcomes only; never archive test stdout or runtime data."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from compare_ci_tests import ANSI, all_passed


def write_inventory(suite, records, seconds=None):
    directory = os.environ.get("CI_INVENTORY_DIR")
    if not directory:
        return False
    if not re.fullmatch(r"[a-z-]+", suite) or not records:
        raise ValueError("Invalid or empty test inventory")
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(
        {"suite": suite, "tests": sorted(records), "pytest_seconds": seconds},
        ensure_ascii=False,
    )
    path = root / (suite + ".json")
    path.write_text(data + "\n")
    path.chmod(0o600)
    print(
        f"CI_INVENTORY {suite}: {len(records)} outcomes; sha256={hashlib.sha256(data.encode()).hexdigest()}",
        flush=True,
    )
    return True


def record_verbose(suite, output):
    records = []
    for line in ANSI.sub("", output).splitlines():
        if match := re.fullmatch(
            r"(tests/.+?::.+?)\s+(PASSED|SKIPPED)(?:\s+\[.*\])?", line.strip()
        ):
            records.append((match[1], match[2]))
    passed = sum(outcome == "PASSED" for _, outcome in records)
    skipped = len(records) - passed
    counts = re.findall(r"\b(\d+) passed(?:,| in)", output)
    skip_counts = re.findall(r"\b(\d+) skipped(?:,| in)", output)
    if counts != [str(passed)] or (skip_counts or ["0"]) != [str(skipped)]:
        raise ValueError("Verbose outcomes do not match the pytest summary")
    seconds = re.findall(r"\bin ([0-9.]+)s", output)
    return write_inventory(suite, records, float(seconds[-1]) if seconds else None)


def record_collection(suite, collected_output, execution_output):
    identifiers = [
        line.strip()
        for line in collected_output.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    seconds = re.findall(r"\bin ([0-9.]+)s", execution_output)
    return write_inventory(
        suite,
        all_passed(identifiers, execution_output),
        float(seconds[-1]) if seconds else None,
    )


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "backend":
        sys.exit("Usage: ci_inventory.py backend EXECUTION_LOG")
    result = subprocess.run(
        ["uv", "run", "--locked", "pytest", "--collect-only", "-q"],
        cwd=Path(__file__).resolve().parents[1] / "backend",
        text=True,
        capture_output=True,
        check=True,
    )
    record_collection("backend", result.stdout, Path(sys.argv[2]).read_text())
