"""Validate and export only the closed synthetic measurement record."""

import json
import os
import re
from pathlib import Path


def emit(output, head, dirty):
    records = re.findall(r"KB_ACCEPTANCE_JSON (.+)", output)
    if len(records) != 1:
        raise ValueError("Missing or duplicate corpus measurement")
    result = json.loads(records[0])
    allowed = {
        "format", "corpus_version", "corpus_sha256", "relevant_count",
        "negative_count", "expected_section_count", "hits", "misses",
        "hit_rate_at_5", "SYNTHETIC_HIT_RATE_AT_5", "mrr_at_5",
        "negative_passed", "acl_incidents", "complete", "faults",
        "technical_result", "SYNTHETIC_CORPUS_VALIDATION",
        "SYNTHETIC_NEGATIVE_SAFETY", "REAL_EXPERT_CORPUS",
        "REAL_TOP5_ACCEPTANCE", "REAL_HIT_RATE_AT_5", "REAL_NEGATIVE_SAFETY",
        "EXPERT_REVIEW", "TARGET_STAGING", "PRODUCTION_ADMISSION",
        "postgresql_version", "head_sha", "dirty", "queries",
        "total_relevant", "hit_at_5_count", "miss_at_5_count", "total_negative",
        "negative_safe_count", "negative_unsafe_count", "failed_query_ids",
        "http_error_count", "db_error_count", "query_error_count",
        "missing_result_count", "measured_corpus_versions", "measured_schema_revisions",
    }
    query_fields = {
        "query_id", "group", "subclass", "expected_section_count", "total",
        "returned_sections", "first_relevant_position", "hit_at_5", "safe",
        "execution_valid",
    }
    if set(result) - allowed or any(
        set(query) - query_fields for query in result.get("queries", [])
    ):
        raise ValueError("Unapproved evidence fields")
    if (
        result.get("format") != "kb-acceptance-result-v1"
        or result.get("head_sha") != head
        or result.get("dirty") != str(dirty).lower()
        or not str(result.get("postgresql_version", "")).startswith("16.")
        or result.get("relevant_count", 0) < 20
        or result.get("negative_count", 0) < 20
        or result.get("negative_passed") != result["negative_count"]
        or result.get("hits", 0) * 5 < result["relevant_count"] * 4
        or result.get("acl_incidents") != 0
        or result.get("complete") is not True
        or result.get("technical_result") != "PASS"
        or result.get("REAL_EXPERT_CORPUS") != "BLOCKED"
        or result.get("REAL_TOP5_ACCEPTANCE") != "BLOCKED"
        or result.get("REAL_HIT_RATE_AT_5") != "NOT_MEASURED"
        or result.get("PRODUCTION_ADMISSION") != "BLOCKED"
        or len(result.get("queries", [])) != result["relevant_count"] + result["negative_count"]
        or len({q["query_id"] for q in result["queries"]}) != len(result["queries"])
        or any(not q.get("execution_valid") for q in result["queries"])
    ):
        raise ValueError("Invalid synthetic corpus measurement")
    print("KB_ACCEPTANCE_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    if directory := os.environ.get("CI_INVENTORY_DIR"):
        path = Path(directory) / "kb047-aggregate.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    return result
