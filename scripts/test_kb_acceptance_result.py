"""Closed evidence channel cannot turn partial or stale runs into acceptance."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from kb_acceptance_result import emit


class CorpusEvidenceTests(unittest.TestCase):
    def record(self):
        return {
            "format": "kb-acceptance-result-v1", "head_sha": "a" * 40,
            "dirty": "false", "postgresql_version": "16.13",
            "relevant_count": 20, "negative_count": 20, "negative_passed": 20,
            "hits": 16, "acl_incidents": 0, "complete": True,
            "technical_result": "PASS", "REAL_EXPERT_CORPUS": "BLOCKED",
            "REAL_TOP5_ACCEPTANCE": "BLOCKED", "REAL_HIT_RATE_AT_5": "NOT_MEASURED",
            "PRODUCTION_ADMISSION": "BLOCKED",
            "queries": [{"query_id": f"Q{i}", "execution_valid": True} for i in range(40)],
        }

    def test_rejects_missing_duplicate_stale_and_partial_evidence(self):
        good = self.record()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(emit("KB_ACCEPTANCE_JSON " + json.dumps(good), "a" * 40, False), good)
            for change in (
                {"head_sha": "b" * 40}, {"dirty": "true"}, {"hits": 15},
                {"acl_incidents": 1}, {"complete": False},
                {"negative_passed": 19}, {"REAL_EXPERT_CORPUS": "APPROVED"},
                {"queries": good["queries"][:-1]},
                {"excerpt": "SYNTHETIC ONLY private text must not be exported"},
            ):
                with self.subTest(change=list(change)), self.assertRaises(ValueError):
                    emit("KB_ACCEPTANCE_JSON " + json.dumps(good | change), "a" * 40, False)
            for output in ("", ("KB_ACCEPTANCE_JSON " + json.dumps(good) + "\n") * 2):
                with self.assertRaises(ValueError): emit(output, "a" * 40, False)
