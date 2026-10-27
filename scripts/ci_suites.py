"""Bounded acceptance scheduling and timings; no application/test substitutions."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from time import monotonic

_summary_lock = Lock()


@contextmanager
def measure(name):
    started = monotonic()
    outcome = "failed"
    print("CI_SUITE_START " + json.dumps({"suite": name}), flush=True)
    try:
        yield
        outcome = "passed"
    finally:
        seconds = round(monotonic() - started, 2)
        print(
            "CI_SUITE_END "
            + json.dumps({"suite": name, "seconds": seconds, "outcome": outcome}),
            flush=True,
        )
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with _summary_lock, Path(summary).open("a+") as output:
                if output.tell() == 0:
                    output.write("| Suite | Duration | Result |\n|---|---:|---|\n")
                output.write(f"| {name} | {seconds:.2f} s | {outcome} |\n")


def parallel(suites):
    """Run every selected suite, at most two at once, and propagate any failure."""

    def exercise(name, check):
        with measure(name):
            check()

    failures = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {
            executor.submit(exercise, name, check): name for name, check in suites
        }
        for future in as_completed(pending):
            if error := future.exception():
                failures.append((pending[future], error))
    if failures:
        raise RuntimeError(
            "Failed suites: " + ", ".join(n for n, _ in failures)
        ) from failures[0][1]


def command(*args):
    # Buffer each command separately so parallel verbose test IDs remain intact.
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    print(result.stdout, flush=True)
    print(result.stderr, file=sys.stderr, flush=True)
    result.check_returncode()


if __name__ == "__main__":
    if sys.argv[1:] == ["compose"]:
        parallel(
            [
                ("compose:smoke", lambda: command("make", "smoke")),
                ("compose:worker-smtp", lambda: command("make", "worker-smoke")),
            ]
        )
    elif len(sys.argv) > 3 and sys.argv[2] == "--":
        with measure(sys.argv[1]):
            command(*sys.argv[3:])
    else:
        sys.exit("Usage: ci_suites.py compose | NAME -- COMMAND [ARG ...]")
