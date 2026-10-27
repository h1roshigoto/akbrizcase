"""Exercise scheduler failure propagation and the shared two-stack budget."""

import io
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ci_suites import measure, parallel


class SchedulerTests(unittest.TestCase):
    @patch("ci_suites.measure", lambda _name: nullcontext())
    def test_all_suites_run_with_no_more_than_two_active(self):
        both_started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = maximum = completed = 0

        def check():
            nonlocal active, maximum, completed
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    both_started.set()
            self.assertTrue(release.wait(5))
            with lock:
                active -= 1
                completed += 1

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(parallel, [(str(i), check) for i in range(7)])
            try:
                self.assertTrue(both_started.wait(5))
                with lock:
                    self.assertEqual(active, 2)
            finally:
                release.set()
            future.result(timeout=5)
        self.assertEqual(maximum, 2)
        self.assertEqual(completed, 7)

    @patch("ci_suites.measure", lambda _name: nullcontext())
    def test_failure_does_not_hide_or_prevent_other_suites(self):
        completed = []

        def fail():
            raise ValueError("synthetic failure")

        with self.assertRaisesRegex(RuntimeError, "Failed suites: broken") as error:
            parallel(
                [
                    ("broken", fail),
                    ("second", lambda: completed.append("second")),
                    ("third", lambda: completed.append("third")),
                ]
            )
        self.assertIsInstance(error.exception.__cause__, ValueError)
        self.assertCountEqual(completed, ["second", "third"])

    def test_failed_duration_is_visible_in_log_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "summary.md"
            output = io.StringIO()
            with (
                patch.dict(os.environ, GITHUB_STEP_SUMMARY=str(summary)),
                redirect_stdout(output),
                self.assertRaises(ValueError),
                measure("synthetic"),
            ):
                raise ValueError("synthetic failure")
            self.assertIn('"outcome": "failed"', output.getvalue())
            self.assertIn("| synthetic |", summary.read_text())
            self.assertIn("failed", summary.read_text())


if __name__ == "__main__":
    unittest.main()
