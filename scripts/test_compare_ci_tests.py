"""The CI evidence comparison must detect missing IDs despite equal counts."""

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from compare_ci_tests import RANDOM_UUID_CASES, compare, inventory, normalize_id


class InventoryTests(unittest.TestCase):
    def test_artifacts_are_complete_and_do_not_double_count_console_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job in ("backend", "frontend", "database", "compose"):
                (root / (job + ".log")).write_text("tests/a.py::one PASSED [100%]\n")
            names = [
                "backend",
                "db-clean-upgrade",
                "db-core",
                "db-images",
                "db-lifecycle",
                "db-applicability",
                "db-trees",
                "files",
            ]
            for name in names:
                (root / (name + ".json")).write_text(
                    json.dumps(
                        {"suite": name, "tests": [["tests/a.py::one", "PASSED"]]}
                    )
                )
            result = inventory(root)
            self.assertEqual(result["backend"].total(), 1)
            self.assertEqual(result["database"].total(), 7)
            (root / "db-trees.json").unlink()
            with self.assertRaisesRegex(ValueError, "Missing inventory"):
                inventory(root)

    def test_file_inventory_needs_its_own_complete_execution_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job in ("backend", "frontend", "compose"):
                (root / (job + ".log")).write_text("tests/a.py::one PASSED [100%]\n")
            database = root / "database.log"
            database.write_text(
                "tests/integration/db.py::one PASSED [100%]\n"
                "CI_FILE_TEST_RESULTS_BEGIN\n1 passed in 1.0s\nCI_FILE_TEST_RESULTS_END\n"
                "CI_FILE_TEST_INVENTORY_BEGIN\ntests/files.py::one\nCI_FILE_TEST_INVENTORY_END\n"
            )
            self.assertEqual(inventory(root)["database"].total(), 2)
            database.write_text(database.read_text().replace("1 passed", "0 passed"))
            with self.assertRaisesRegex(ValueError, "Execution"):
                inventory(root)

    def test_collection_is_evidence_only_when_the_full_execution_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job in ("frontend", "database", "compose"):
                (root / (job + ".log")).write_text("tests/a.py::one PASSED [100%]\n")
            backend = root / "backend.log"
            ids = "tests/a.py::one\ntests/a.py::two\n"
            backend.write_text("=== 2 passed in 1.0s ===\n" + ids)
            self.assertEqual(inventory(root)["backend"].total(), 2)
            for summary in (
                "=== 1 passed in 1.0s ===\n",
                "=== 1 passed, 1 skipped in 1.0s ===\n",
                "=== 2 passed, 1 skipped in 1.0s ===\n",
            ):
                backend.write_text(summary + ids)
                with self.assertRaisesRegex(ValueError, "Execution"):
                    inventory(root)

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
