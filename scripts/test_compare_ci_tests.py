"""The CI evidence comparison must detect missing IDs despite equal counts."""

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from compare_ci_tests import RANDOM_UUID_CASES, compare, inventory, normalize_id


class InventoryTests(unittest.TestCase):
    def test_only_known_collection_time_uuid4_parameters_are_normalized(self):
        value = "12345678-1234-4123-8123-123456789abc"
        for prefix in RANDOM_UUID_CASES:
            self.assertEqual(normalize_id(prefix + value + "]"), prefix + "<uuid4>]")
        other = "tests/other.py::case[" + value + "]"
        self.assertEqual(normalize_id(other), other)
        non_v4 = RANDOM_UUID_CASES[0] + value.replace("-4123-", "-1123-") + "]"
        self.assertEqual(normalize_id(non_v4), non_v4)

    def test_equal_counts_cannot_hide_a_replaced_test_or_changed_outcome(self):
        before = {"backend": Counter({("tests/a.py::one", "PASSED"): 1})}
        for replacement in (
            ("tests/a.py::two", "PASSED"),
            ("tests/a.py::one", "SKIPPED"),
        ):
            result = compare(before, {"backend": Counter({replacement: 1})})["backend"]
            self.assertTrue(result["missing_or_changed"])
            self.assertTrue(result["added_or_changed"])

    def test_log_parsing_preserves_parameters_duplicates_and_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job in ("backend", "frontend", "database", "compose"):
                (root / (job + ".log")).write_text(
                    "2026-10-27T19:00:00.123Z tests/a.py::one[with spaces] PASSED [ 50%]\n"
                    "tests/a.py::one[with spaces] PASSED [100%]\n"
                    "tests/a.py::two SKIPPED [100%]\n"
                    "PASSED tests/a.py::summary[with spaces]\n"
                    " ✓ src/test/a.ts > group > title 23ms\n"
                    "PASS: browser HEAD=" + "a" * 40 + "\n"
                )
            results = inventory(root)
            self.assertEqual(
                results["backend"][("tests/a.py::one[with spaces]", "PASSED")], 2
            )
            self.assertEqual(results["backend"][("tests/a.py::two", "SKIPPED")], 1)
            self.assertEqual(
                results["backend"][("tests/a.py::summary[with spaces]", "PASSED")], 1
            )
            self.assertEqual(
                results["frontend"][("src/test/a.ts > group > title", "PASSED")], 1
            )
            self.assertEqual(
                results["compose"][("PASS: browser HEAD=<sha>", "PASSED")], 1
            )


if __name__ == "__main__":
    unittest.main()
