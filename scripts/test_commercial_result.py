"""Critical acceptance cannot silently succeed with missing/ignored cases."""

import unittest

from commercial_result import EXPECTED, PREFIX, acceptance


def completed():
    records = [
        f"{PREFIX}{name}[{index}] PASSED"
        for name, count in EXPECTED.items()
        for index in range(count)
    ]
    return (
        f"collected {len(records)} items\n"
        + "\n".join(records)
        + f"\n=== {len(records)} passed in 1.23s ===\n"
    )


class CommercialAcceptanceTests(unittest.TestCase):
    def test_complete_execution_records_exact_head_and_groups(self):
        result = acceptance(completed(), "a" * 40)
        self.assertEqual(result["groups"], EXPECTED)
        self.assertEqual(result["passed"], sum(EXPECTED.values()))
        self.assertEqual(result["head"], "a" * 40)
        self.assertFalse(result["worktree_dirty"])

    def test_collection_skips_xfail_deselection_duplicates_and_missing_group_fail(self):
        output = completed()
        first = next(iter(EXPECTED))
        for changed in (
            "collected 64 items\n",  # collection alone is not acceptance
            output.replace("PASSED", "SKIPPED", 1),
            output.replace("PASSED", "XFAIL", 1),
            output.replace("PASSED", "XPASS", 1),
            output.replace("PASSED", "FAILED", 1),
            output.replace(f"{first}[0]", f"{first}[1]", 1),
            output.replace(first, "test_unrelated"),
            output + "1 deselected\n",
            output.replace("64 passed", "63 passed, 1 skipped"),
            output.replace("collected 64", "collected 65"),
            output.replace(PREFIX, "tests/integration/test_order_creation.py::", 1),
        ):
            with self.subTest(kind=changed[-70:]), self.assertRaises(ValueError):
                acceptance(changed, "a" * 40)

    def test_invalid_head_is_rejected(self):
        with self.assertRaises(ValueError):
            acceptance(completed(), "main")


if __name__ == "__main__":
    unittest.main()
