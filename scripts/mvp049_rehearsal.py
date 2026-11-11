"""Closed receipt for actual workflow regressions; synthetic only, never rights."""

import re

from mvp049_content_gate import fields, git, require

PILOT_STAGES = {
    "pilot-catalog": (
        "tests/integration/test_catalog_writes.py::test_item_model_lifecycle_and_public_search",
    ),
    "pilot-applicability": (
        "tests/integration/test_catalog_applicability.py::test_workflow_revision_noop_replace_and_re_review",
        "tests/integration/test_catalog_applicability.py::test_sqladmin_review_disclosure_and_internal_privacy",
    ),
    "pilot-paths": (
        "tests/integration/test_acl_publication_regression.py::test_every_shipped_graph_path_through_published_engine_http_and_dependency_loss",
        "tests/integration/test_acl_publication_regression.py::test_linked_revoke_propagates_to_all_public_surfaces",
        "tests/integration/test_acl_publication_regression.py::test_retired_exact_pin_then_revoke_and_new_publication_never_resurrects_permission",
        "tests/integration/test_acl_publication_regression.py::test_internal_evidence_confirms_fact_without_leaks_in_catalog_commerce_and_replay",
    ),
    # The normal runner additionally validates every locked 20+20 outcome,
    # denominator, actual PG16 version, schema, HEAD and all ACL incidents.
    "pilot-search": ("tests/integration/test_kb_acceptance.py",),
}


def receipt(repo, head, counts, fts):
    result = {
        "format": "MVP049_REHEARSAL/v1",
        "classification": "SYNTHETIC ONLY",
        "head": head,
        "source_tree": git(repo, "rev-parse", "HEAD^{tree}"),
        "stages": counts,
        "fts": fts,
        "scope": "WORKFLOW_REGRESSION_ONLY",
        "PILOT_CONTENT_ADMISSION": "BLOCKED",
        "PRODUCTION_ADMISSION": "BLOCKED",
    }
    validate_receipt(result, repo, head)
    return result


def validate_receipt(value, repo, head):
    """Validate structure/bindings only; JSON does not prove test execution."""
    # Imports are lazy to keep ordinary guard/preflight startup offline and small.
    from kb_acceptance_result import CORPUS_SHA256
    from operator_rehearsal import STAGES

    fields(
        value,
        "format classification head source_tree stages fts scope "
        "PILOT_CONTENT_ADMISSION PRODUCTION_ADMISSION",
    )
    require(
        value["format"] == "MVP049_REHEARSAL/v1"
        and value["classification"] == "SYNTHETIC ONLY"
        and value["head"] == head
        and value["source_tree"] == git(repo, "rev-parse", "HEAD^{tree}")
        and value["scope"] == "WORKFLOW_REGRESSION_ONLY"
        and value["PILOT_CONTENT_ADMISSION"]
        == value["PRODUCTION_ADMISSION"]
        == "BLOCKED",
        "REHEARSAL_INVALID",
    )
    stages = {**STAGES, **PILOT_STAGES}
    require(
        type(value["stages"]) is dict and set(value["stages"]) == set(stages),
        "REHEARSAL_INCOMPLETE",
    )
    for stage, count in value["stages"].items():
        require(
            type(count) is int and count >= len(stages[stage]), "REHEARSAL_INCOMPLETE"
        )
    fts = value["fts"]
    fields(fts, "corpus_sha256 relevant negative hits negative_safe acl_incidents")
    require(
        fts["corpus_sha256"] == CORPUS_SHA256
        and all(type(fts[k]) is int for k in fts if k != "corpus_sha256")
        and fts["relevant"] == 20
        and fts["negative"] == fts["negative_safe"] == 20
        and 16 <= fts["hits"] <= 20
        and fts["acl_incidents"] == 0,
        "REHEARSAL_FTS_INVALID",
    )
    return value


def passed_count(output):
    summaries = re.findall(r"=+ ([^\n]*\d+ passed[^\n]*) =+", output)
    require(
        len(summaries) == 1
        and not re.search(r"\b(skipped|xfailed|xpassed|failed|error)\b", summaries[0]),
        "REHEARSAL_INCOMPLETE",
    )
    return int(re.search(r"(\d+) passed", summaries[0]).group(1))


def check_schema_fixture(repo, head, record, directory):
    """Copy public test inputs into an explicitly private rehearsal namespace.

    This checker grants no execution PASS. It is exclusively a schema fixture
    and never represents the ephemeral DB
    entities used by the independent workflow regressions or real pilot content.
    """
    import json
    from pathlib import Path

    from mvp049_content_gate import PrivateInput, canonical, evaluate, sha

    source = Path(repo) / "content/pilot/synthetic"
    manifest = json.loads((source / "manifest.template.json").read_bytes())
    require(manifest["classification"] == "SYNTHETIC ONLY", "SYNTHETIC_ESCALATION")
    directory.mkdir(mode=0o700)
    for entry in manifest["artifacts"]:
        data = (source / entry["filename"]).read_bytes()
        require(sha(data) == entry["sha256"], "CHECKSUM_MISMATCH")
        path = directory / entry["filename"]
        with path.open("xb") as stream:
            path.chmod(0o600)
            stream.write(data)
    manifest["git"].update(head=head, base=head)
    manifest["rehearsal_artifact"] = "workflow-receipt"
    data = canonical(record)
    path = directory / "workflow-receipt.json"
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(data)
    manifest["artifacts"].append(
        dict(
            id="workflow-receipt",
            filename=path.name,
            sha256=sha(data),
            kind="rehearsal",
        )
    )
    data = canonical(manifest)
    path = directory / "manifest.json"
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(data)
    packet = PrivateInput(directory, repo)
    try:
        result = evaluate(packet, "manifest.json", sha(data), repo, head, head)
    finally:
        packet.close()
    result["codes"].extend(["SCHEMA_FIXTURE_ONLY", "WORKFLOW_REGRESSION_ONLY"])
    return result
