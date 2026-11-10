"""Closed evidence channel cannot turn forged outcomes into acceptance."""

import contextlib
import copy
import io
import json
import os
import stat
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from kb_acceptance_result import CORPUS_SHA256, emit, locked_corpus, schema_revisions


class CorpusEvidenceTests(unittest.TestCase):
    def record(self, hits=16):
        manifest, queries, _ = locked_corpus()
        rows = []
        relevant_index = 0
        for q in queries:
            relevant = q["group"] == "relevant"
            keys = q["expected_sections"][:1] if relevant else q["safe_sections"][:1]
            if relevant:
                relevant_index += 1
                if relevant_index > hits:
                    keys = []
            rows.append(
                dict(
                    query_id=q["query_id"],
                    group=q["group"],
                    subclass=q["subclass"],
                    expected_section_count=len(q["expected_sections"]),
                    total=len(keys),
                    returned_sections=keys,
                    first_relevant_position=1 if relevant and keys else None,
                    hit_at_5=int(bool(keys)) if relevant else None,
                    safe=True,
                    execution_valid=True,
                    reply_valid=True,
                    reply_resolution="results" if keys else "no_match",
                    http_error=False,
                    db_error=False,
                    query_error=False,
                    result_missing=False,
                    page_acl_incidents=0,
                    reply_acl_incidents=0,
                )
            )
        return {
            "format": "kb-acceptance-result-v1",
            "head_sha": "a" * 40,
            "dirty": "false",
            "postgresql_version": "16.13 (Debian build)",
            "corpus_version": manifest["corpus_version"],
            "corpus_sha256": CORPUS_SHA256,
            "relevant_count": 20,
            "total_relevant": 20,
            "negative_count": 20,
            "total_negative": 20,
            "expected_section_count": 21,
            "hits": hits,
            "hit_at_5_count": hits,
            "misses": 20 - hits,
            "miss_at_5_count": 20 - hits,
            "hit_rate_at_5": float(Fraction(hits, 20)),
            "SYNTHETIC_HIT_RATE_AT_5": float(Fraction(hits, 20)),
            "mrr_at_5": float(Fraction(hits, 20)),
            "negative_passed": 20,
            "negative_safe_count": 20,
            "negative_unsafe_count": 0,
            "acl_incidents": 0,
            "page_acl_incidents": 0,
            "reply_acl_incidents": 0,
            "http_error_count": 0,
            "db_error_count": 0,
            "query_error_count": 0,
            "missing_result_count": 0,
            "failed_query_ids": [r["query_id"] for r in rows if r["hit_at_5"] == 0],
            "measured_corpus_versions": [
                {"version": manifest["corpus_version"], "sha256": CORPUS_SHA256}
            ],
            "measured_schema_revisions": schema_revisions(),
            "complete": True,
            "faults": [],
            "technical_result": "PASS",
            "SYNTHETIC_CORPUS_VALIDATION": "PASS",
            "SYNTHETIC_NEGATIVE_SAFETY": "PASS",
            "REAL_EXPERT_CORPUS": "BLOCKED",
            "REAL_TOP5_ACCEPTANCE": "BLOCKED",
            "REAL_HIT_RATE_AT_5": "NOT_MEASURED",
            "REAL_NEGATIVE_SAFETY": "BLOCKED",
            "EXPERT_REVIEW": "PENDING",
            "TARGET_STAGING": "BLOCKED",
            "PRODUCTION_ADMISSION": "BLOCKED",
            "queries": rows,
        }

    def assert_rejected(self, record):
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            output = (
                record
                if isinstance(record, str)
                else "KB_ACCEPTANCE_JSON " + json.dumps(record)
            )
            with (
                patch.dict(os.environ, {"CI_INVENTORY_DIR": directory}),
                contextlib.redirect_stdout(stdout),
            ):
                with self.assertRaises(ValueError):
                    emit(output, "a" * 40, False)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_complete_outcomes_threshold_and_inventory(self):
        for hits in (16, 18, 20):
            good = self.record(hits)
            with self.subTest(hits=hits), tempfile.TemporaryDirectory() as directory:
                with (
                    patch.dict(
                        os.environ,
                        {"CI_INVENTORY_DIR": str(Path(directory) / "inventory")},
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        emit("KB_ACCEPTANCE_JSON " + json.dumps(good), "a" * 40, False),
                        good,
                    )
                path = Path(directory) / "inventory" / "kb047-aggregate.json"
                self.assertEqual(json.loads(path.read_text()), good)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_forged_aggregates_and_all_duplicate_totals(self):
        for key, value in {
            "hits": 20,
            "hit_at_5_count": 20,
            "misses": 0,
            "miss_at_5_count": 0,
            "hit_rate_at_5": 1.0,
            "SYNTHETIC_HIT_RATE_AT_5": 1.0,
            "mrr_at_5": 1.0,
            "negative_passed": 19,
            "negative_safe_count": 19,
            "negative_unsafe_count": 1,
            "relevant_count": 21,
            "total_relevant": 19,
            "negative_count": 21,
            "total_negative": 19,
            "expected_section_count": 20,
            "failed_query_ids": [],
            "acl_incidents": 1,
            "page_acl_incidents": 1,
            "reply_acl_incidents": 1,
            "http_error_count": 1,
            "db_error_count": 1,
            "query_error_count": 1,
            "missing_result_count": 1,
        }.items():
            with self.subTest(key=key):
                self.assert_rejected(self.record() | {key: value})
        # Previous emit accepted 20 aggregate hits backed by only 15 row hits.
        self.assert_rejected(self.record(15) | {"hits": 20})
        self.assert_rejected(self.record(15))

    def test_unsafe_negative_cannot_hide_behind_twenty_passed(self):
        for index in (20, 39):  # no_match and safe_quote
            good = self.record(20)
            row = good["queries"][index]
            row.update(
                total=1,
                returned_sections=["indicator"],
                reply_resolution="results",
                safe=False,
            )
            self.assert_rejected(good)
            row["safe"] = True  # lying about safety also fails independently
            self.assert_rejected(good)

    def test_rank_uses_actual_section_positions(self):
        good = self.record(20)
        good["queries"][0].update(
            total=2,
            returned_sections=["filters", "indicator"],
            first_relevant_position=2,
        )
        good["mrr_at_5"] = float((Fraction(19) + Fraction(1, 2)) / 20)
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            emit("KB_ACCEPTANCE_JSON " + json.dumps(good), "a" * 40, False)
        good["queries"][0]["first_relevant_position"] = 1
        self.assert_rejected(good)

    def test_row_schema_identity_and_semantics(self):
        for change in (
            {"first_relevant_position": 2},
            {"first_relevant_position": True},
            {"hit_at_5": 0},
            {"hit_at_5": True},
            {"safe": False},
            {"safe": 1},
            {"execution_valid": False},
            {"execution_valid": "true"},
            {"reply_valid": False},
            {"reply_resolution": "no_match"},
            {"reply_resolution": "handoff"},
            {"http_error": True},
            {"db_error": True},
            {"query_error": True},
            {"result_missing": True},
            {"page_acl_incidents": 1},
            {"reply_acl_incidents": 1},
            {"group": "unsafe"},
            {"subclass": "forged"},
            {"expected_section_count": 2},
            {"total": 0},
            {"total": True},
            {"total": 1.0},
            {"returned_sections": []},
            {"returned_sections": ["unknown"]},
            {"returned_sections": [None]},
            {"returned_sections": ["internal"]},
            {"returned_sections": ["indicator", "indicator"], "total": 2},
            {"query_id": "forged"},
            {"query_id": "R02"},
            {"excerpt": "must not export"},
        ):
            with self.subTest(change=change):
                good = self.record(20)
                good["queries"][0].update(change)
                self.assert_rejected(good)
        for field in self.record()["queries"][0]:
            with self.subTest(missing_field=field):
                good = self.record()
                del good["queries"][0][field]
                self.assert_rejected(good)
        for rows in (
            None,
            {},
            [None] * 40,
            ["forged"] * 40,
            [],
            self.record()["queries"][:-1],
        ):
            self.assert_rejected(self.record() | {"queries": rows})
        for change in ({"hit_at_5": 0}, {"first_relevant_position": 1}):
            good = self.record()
            good["queries"][-1].update(change)
            self.assert_rejected(good)

    def test_closed_top_level_schema_provenance_and_statuses(self):
        good = self.record()
        for field in good:
            with self.subTest(missing_field=field):
                bad = copy.deepcopy(good)
                del bad[field]
                self.assert_rejected(bad)
        for key, value in {
            "head_sha": "b" * 40,
            "dirty": "true",
            "postgresql_version": "15.9",
            "corpus_version": "1.0.0",
            "corpus_sha256": "0" * 64,
            "measured_corpus_versions": [{"version": "1.1.0", "sha256": "0" * 64}],
            "measured_schema_revisions": ["invented"],
            "complete": 1,
            "faults": ["MISSING_OR_INVALID_RESULT"],
            "technical_result": "FAIL",
            "SYNTHETIC_CORPUS_VALIDATION": "FAIL",
            "SYNTHETIC_NEGATIVE_SAFETY": "FAIL",
            "REAL_EXPERT_CORPUS": "APPROVED",
            "REAL_TOP5_ACCEPTANCE": "PASS",
            "REAL_HIT_RATE_AT_5": 1.0,
            "REAL_NEGATIVE_SAFETY": "PASS",
            "EXPERT_REVIEW": "APPROVED",
            "TARGET_STAGING": "PASS",
            "PRODUCTION_ADMISSION": "PASS",
            "failed_query_ids": ["R17"] * 4,
            "private_text": "must not export",
        }.items():
            with self.subTest(key=key):
                self.assert_rejected(good | {key: value})
        for field in ("hits", "negative_passed", "relevant_count", "acl_incidents"):
            for value in (True, "20", 20.0, None, -1, float("nan"), float("inf")):
                self.assert_rejected(good | {field: value})
        for field in ("hit_rate_at_5", "mrr_at_5"):
            for value in (True, "0.8", float("nan"), float("inf")):
                self.assert_rejected(good | {field: value})

    def test_strict_json_and_no_output_on_any_failure(self):
        encoded = json.dumps(self.record())
        for data in (
            "",
            "KB_ACCEPTANCE_JSON " + encoded + "\nKB_ACCEPTANCE_JSON " + encoded,
            "KB_ACCEPTANCE_JSON []",
            "KB_ACCEPTANCE_JSON null",
            "KB_ACCEPTANCE_JSON {",
            'KB_ACCEPTANCE_JSON {"hits": 15, ' + encoded[1:],
            "KB_ACCEPTANCE_JSON "
            + encoded.replace(
                '"query_id": "R01"', '"query_id": "R01", "query_id": "R01"'
            ),
            "KB_ACCEPTANCE_JSON "
            + encoded.replace('"mrr_at_5": 0.8', '"mrr_at_5": 1e400'),
        ):
            self.assert_rejected(data)
