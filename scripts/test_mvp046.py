"""Fail-closed evidence guards, independent of Docker and browser execution."""

import unittest

from mvp046_browser_smoke import CASES, validate_result


class EvidenceTests(unittest.TestCase):
    def test_complete_linked_result(self):
        validate_result(list(CASES), [5, 5, 4, 5, 7], [1, 2, 3, 4, 5])

    def test_missing_or_duplicate_journey_is_not_success(self):
        for cases in (
            list(CASES)[:-1],
            [*CASES, next(iter(CASES))],
            [*CASES, "invented"],
        ):
            with self.subTest(cases=len(cases)), self.assertRaises(AssertionError):
                validate_result(cases, [5, 5, 4, 5, 7], [1, 2, 3, 4, 5])

    def test_partial_or_duplicate_business_graph_is_not_success(self):
        for counts in (
            [6, 6, 4, 6, 8],
            [5, 4, 4, 5, 7],
            [5, 5, 5, 5, 7],
            [5, 5, 4, 4, 7],
            [5, 5, 4, 5, 6],
        ):
            with self.subTest(counts=counts), self.assertRaises(AssertionError):
                validate_result(list(CASES), counts, [1, 2, 3, 4, 5])
        with self.assertRaises(AssertionError):
            validate_result(list(CASES), [5, 5, 4, 5, 7], [1, 2, 3, 4, 4])
