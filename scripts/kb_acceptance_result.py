"""Validate the complete locked synthetic outcomes before exporting evidence.

This stdlib-only boundary runs on the host, outside the backend environment.
No query text, source UUID, excerpt or private external evidence is accepted.
"""

import ast
import hashlib
import json
import math
import os
import re
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "backend/tests/fixtures/kb_acceptance"
CORPUS_SHA256 = "903071740ae8518e36ae051115dfed99beb1cbd3a49f6a24be4541f1790d797d"
FIELDS = {
    "format",
    "corpus_version",
    "corpus_sha256",
    "relevant_count",
    "negative_count",
    "expected_section_count",
    "hits",
    "misses",
    "hit_rate_at_5",
    "SYNTHETIC_HIT_RATE_AT_5",
    "mrr_at_5",
    "negative_passed",
    "acl_incidents",
    "page_acl_incidents",
    "reply_acl_incidents",
    "complete",
    "faults",
    "technical_result",
    "SYNTHETIC_CORPUS_VALIDATION",
    "SYNTHETIC_NEGATIVE_SAFETY",
    "REAL_EXPERT_CORPUS",
    "REAL_TOP5_ACCEPTANCE",
    "REAL_HIT_RATE_AT_5",
    "REAL_NEGATIVE_SAFETY",
    "EXPERT_REVIEW",
    "TARGET_STAGING",
    "PRODUCTION_ADMISSION",
    "postgresql_version",
    "head_sha",
    "dirty",
    "queries",
    "total_relevant",
    "hit_at_5_count",
    "miss_at_5_count",
    "total_negative",
    "negative_safe_count",
    "negative_unsafe_count",
    "failed_query_ids",
    "http_error_count",
    "db_error_count",
    "query_error_count",
    "missing_result_count",
    "measured_corpus_versions",
    "measured_schema_revisions",
}
QUERY_FIELDS = {
    "query_id",
    "group",
    "subclass",
    "expected_section_count",
    "total",
    "returned_sections",
    "first_relevant_position",
    "hit_at_5",
    "safe",
    "execution_valid",
    "reply_valid",
    "reply_resolution",
    "http_error",
    "db_error",
    "query_error",
    "result_missing",
    "page_acl_incidents",
    "reply_acl_incidents",
}


def require(condition):
    if not condition:
        # Do not include untrusted fields or protected input in diagnostics.
        raise ValueError("Invalid synthetic corpus measurement")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def strict_json(data):
    return json.loads(
        data, object_pairs_hook=unique_object, parse_constant=reject_constant
    )


def locked_corpus():
    data = (CORPUS / "manifest.json").read_bytes()
    require(hashlib.sha256(data).hexdigest() == CORPUS_SHA256)
    manifest = strict_json(data)
    queries_data = (CORPUS / "queries.json").read_bytes()
    sections_data = (CORPUS / "sections.json").read_bytes()
    for data, checksum in (
        (queries_data, manifest["queries_sha256"]),
        (sections_data, manifest["sections_sha256"]),
    ):
        require(hashlib.sha256(data).hexdigest() == checksum)
    for document in manifest["documents"]:
        require(
            hashlib.sha256((CORPUS / document["material"]).read_bytes()).hexdigest()
            == document["sha256"]
        )
    return manifest, strict_json(queries_data), strict_json(sections_data)


def schema_revisions():
    revisions, parents = set(), set()
    for path in (ROOT / "backend/migrations/versions").glob("*.py"):
        values = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in {"revision", "down_revision"}:
                    values[name] = ast.literal_eval(node.value)
        revisions.add(values["revision"])
        if values["down_revision"] is not None:
            parents.add(values["down_revision"])
    return sorted(revisions - parents)


def count(value):
    require(type(value) is int and value >= 0)
    return value


def validate(result, head, dirty):
    require(type(result) is dict and set(result) == FIELDS)
    manifest, queries, sections = locked_corpus()
    require(
        result["format"] == "kb-acceptance-result-v1"
        and type(head) is str
        and re.fullmatch(r"[0-9a-f]{40}", head) is not None
        and type(dirty) is bool
        and result["head_sha"] == head
        and result["dirty"] == str(dirty).lower()
        and type(result["postgresql_version"]) is str
        and re.fullmatch(r"16\.\d+(?:\s[^\r\n]+)?", result["postgresql_version"])
        is not None
        and result["corpus_version"] == manifest["corpus_version"]
        and result["corpus_sha256"] == CORPUS_SHA256
        and result["measured_corpus_versions"]
        == [{"version": manifest["corpus_version"], "sha256": CORPUS_SHA256}]
        and result["measured_schema_revisions"] == schema_revisions()
        and result["complete"] is True
        and result["faults"] == []
    )
    for key, expected in {
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
    }.items():
        require(result[key] == expected)
    rows = result["queries"]
    require(type(rows) is list and len(rows) == len(queries))
    by_id = {}
    specs = {q["query_id"]: q for q in queries}
    admissible = set()
    for section in sections:
        document = next(
            d
            for d in manifest["documents"]
            if (d["key"], d["version"]) == (section["document_key"], section["version"])
        )
        if (
            document["visibility"] == "public"
            and document["status"] == "active"
            and document["version_status"] == "published"
            and section["assistant_allowed"] is True
        ):
            admissible.add(section["key"])
    hits, negative_passed, reciprocal = 0, 0, Fraction(0)
    failed = set()
    for row in rows:
        require(type(row) is dict and set(row) == QUERY_FIELDS)
        query_id = row["query_id"]
        require(type(query_id) is str and query_id in specs and query_id not in by_id)
        by_id[query_id] = row
        spec = specs[query_id]
        require(row["group"] == spec["group"] and row["subclass"] == spec["subclass"])
        require(count(row["expected_section_count"]) == len(spec["expected_sections"]))
        for key in (
            "execution_valid",
            "reply_valid",
            "safe",
            "http_error",
            "db_error",
            "query_error",
            "result_missing",
        ):
            require(type(row[key]) is bool)
        require(row["execution_valid"] and row["reply_valid"])
        require(
            not any(
                row[k]
                for k in ("http_error", "db_error", "query_error", "result_missing")
            )
        )
        require(
            count(row["page_acl_incidents"]) == count(row["reply_acl_incidents"]) == 0
        )
        total, returned = count(row["total"]), row["returned_sections"]
        require(type(returned) is list and len(returned) == min(5, total))
        require(all(type(key) is str and key in admissible for key in returned))
        require(len(set(returned)) == len(returned))
        resolution = row["reply_resolution"]
        require(resolution in {"results", "no_match", "handoff"})
        if resolution == "handoff":
            require(spec["negative_behavior"] == "handoff")
        else:
            require(resolution == ("results" if returned else "no_match"))
        rank = next(
            (
                i
                for i, key in enumerate(returned, 1)
                if key in spec["expected_sections"]
            ),
            None,
        )
        if spec["group"] == "relevant":
            require(
                row["first_relevant_position"] is None
                or (
                    type(row["first_relevant_position"]) is int
                    and 1 <= row["first_relevant_position"] <= 5
                )
            )
            require(row["first_relevant_position"] == rank)
            require(count(row["hit_at_5"]) == int(rank is not None))
            hits += int(rank is not None)
            reciprocal += Fraction(1, rank) if rank else 0
            safe = True
            if rank is None:
                failed.add(query_id)
        else:
            require(row["first_relevant_position"] is None and row["hit_at_5"] is None)
            safe = (
                (spec["negative_behavior"] == "no_match" and total == 0)
                or (
                    spec["negative_behavior"] == "safe_quote"
                    and bool(returned)
                    and set(returned) <= set(spec["safe_sections"])
                )
                or (spec["negative_behavior"] == "handoff" and resolution == "handoff")
            )
            negative_passed += int(safe)
            if not safe:
                failed.add(query_id)
        require(row["safe"] is safe)
    require(set(by_id) == set(specs))
    relevant = sum(q["group"] == "relevant" for q in queries)
    negative = len(queries) - relevant
    expected_counts = {
        "relevant_count": relevant,
        "total_relevant": relevant,
        "negative_count": negative,
        "total_negative": negative,
        "expected_section_count": sum(len(q["expected_sections"]) for q in queries),
        "hits": hits,
        "hit_at_5_count": hits,
        "misses": relevant - hits,
        "miss_at_5_count": relevant - hits,
        "negative_passed": negative_passed,
        "negative_safe_count": negative_passed,
        "negative_unsafe_count": negative - negative_passed,
        "http_error_count": sum(row["http_error"] for row in rows),
        "db_error_count": sum(row["db_error"] for row in rows),
        "query_error_count": sum(row["query_error"] for row in rows),
        "missing_result_count": sum(row["result_missing"] for row in rows),
        "page_acl_incidents": sum(row["page_acl_incidents"] for row in rows),
        "reply_acl_incidents": sum(row["reply_acl_incidents"] for row in rows),
        "acl_incidents": sum(
            row["page_acl_incidents"] + row["reply_acl_incidents"] for row in rows
        ),
    }
    for key, expected in expected_counts.items():
        require(count(result[key]) == expected)
    for key, expected in {
        "hit_rate_at_5": float(Fraction(hits, relevant)),
        "SYNTHETIC_HIT_RATE_AT_5": float(Fraction(hits, relevant)),
        "mrr_at_5": float(reciprocal / relevant),
    }.items():
        require(
            type(result[key]) is float
            and math.isfinite(result[key])
            and result[key] == expected
        )
    require(type(result["failed_query_ids"]) is list)
    require(all(type(q) is str for q in result["failed_query_ids"]))
    require(len(set(result["failed_query_ids"])) == len(result["failed_query_ids"]))
    require(set(result["failed_query_ids"]) == failed)
    require(hits * 5 >= relevant * 4 and negative_passed == negative)
    return result


def emit(output, head, dirty):
    records = re.findall(r"KB_ACCEPTANCE_JSON (.+)", output)
    require(len(records) == 1)
    try:
        result = validate(strict_json(records[0]), head, dirty)
    except (KeyError, TypeError, OverflowError, RecursionError):
        raise ValueError("Invalid synthetic corpus measurement") from None
    # All validation completes before either public channel is touched.
    if directory := os.environ.get("CI_INVENTORY_DIR"):
        path = Path(directory) / "kb047-aggregate.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print("KB_ACCEPTANCE_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result
