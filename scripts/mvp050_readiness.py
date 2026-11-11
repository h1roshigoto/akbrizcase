"""Offline MVP-050 checklist consistency, with no admission authority.

Only local Git metadata and protected packet bytes are read. Imported outcomes
and decisions remain unverified, including a structurally perfect PASS packet.
"""

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

from mvp048_result import validate_result as validate_load
from mvp049_content_gate import (
    MAX_JSON,
    PrivateInput,
    QuietParser,
    canonical,
    digest,
    indexed,
    lineage,
    parse,
    require,
    validate,
)
from release import MODES, SCHEMA

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "content/readiness"
REQUIREMENTS = json.loads((DATA / "requirements.json").read_bytes())
HISTORICAL = json.loads((DATA / "historical-baseline.json").read_bytes())
CATALOG = {row["id"]: row for row in REQUIREMENTS}
FEATURES = ("catalog", "orders", "manager", "knowledge", "diagnose", "files", "smtp")
# Only existing runtime switches can support a claimed feature exclusion.
# There is no manager switch; listing its endpoints cannot disable staff access.
EXCLUSION_MODES = {
    "catalog": ("catalog", False),
    "orders": ("orders", False),
    "knowledge": ("knowledge", False),
    "diagnose": ("chat", False),
    "files": ("files", False),
    "smtp": ("smtp", "disabled"),
}
NON_EXCLUDABLE_REQUIREMENTS = frozenset(
    r["id"] for r in REQUIREMENTS if r["feature"] == "core"
) | {"MVP-010", "MVP-013"}
# Closed route/method and UI inventory; actual disabling needs independent proof.
ENDPOINTS = {
    "catalog": (
        "UI /: catalog",
        "GET /api/v1/catalog/items",
        "GET /api/v1/catalog/items/{id}",
    ),
    "orders": ("UI /: request form", "POST /api/v1/orders (new and replay)"),
    "manager": (
        "GET /admin/orders",
        "GET /admin/orders/{id}",
        "POST /admin/orders/{id}/status",
        "POST /admin/orders/{id}/retry-notification",
        "GET /api/v1/admin/orders",
        "GET /api/v1/admin/orders/{id}",
        "PATCH /api/v1/admin/orders/{id}/status",
    ),
    "knowledge": (
        "UI /: knowledge mode",
        "GET /api/v1/knowledge/search",
        "GET /api/v1/documents",
        "GET /api/v1/documents/{id}/versions/{version_id}/file",
        "CHAT MODE knowledge via POST /api/v1/chat/messages",
    ),
    "diagnose": (
        "UI /: chat",
        "GET /api/v1/chat/scenarios",
        "POST /api/v1/chat/sessions",
        "GET /api/v1/chat/sessions/{identifier}",
        "POST /api/v1/chat/messages",
    ),
    "files": (
        "GET /api/v1/catalog/items/{id}/image/{image_key}",
        "GET /admin/catalog/items/{record}/image",
        "GET /admin/catalog/items/{record}/image/preview",
        "POST /admin/catalog/items/{record}/image/replace",
        "POST /admin/catalog/items/{record}/image/remove",
        "GET /api/v1/documents/{id}/versions/{version_id}/file",
        "CLI kb-import",
    ),
    "smtp": ("WORKER SMTP delivery", "POST /admin/orders/{id}/retry-notification"),
}
JOBS = (
    "Backend checks",
    "Frontend checks",
    "PostgreSQL 16 integration",
    "Compose build and smoke",
    "Linked browser E2E acceptance",
    "Synthetic staging release rehearsal",
    "Encrypted PG16 backup and clean restore",
    "Synthetic operator handover rehearsal",
    "Bounded synthetic load and recovery",
)
SUITES = (
    "backend",
    "db-clean-upgrade",
    "db-core",
    "db-images",
    "db-lifecycle",
    "db-applicability",
    "db-trees",
    "db-chat",
    "db-http-security",
    "db-commercial-regression",
    "db-acl-publication-regression",
    "db-kb-acceptance",
    "files",
)
STATES = ("PASS", "FAIL", "BLOCKED", "NOT RUN", "INCONCLUSIVE", "OUT_OF_SCOPE_APPROVED")
SCOPES = ("CI_SYNTHETIC", "LOCAL_SYNTHETIC", "TARGET", "EXTERNAL_DECISION")
HEX40 = {"type": "string", "pattern": "[0-9a-f]{40}"}
HEX64 = {"type": "string", "pattern": "[0-9a-f]{64}"}
OPAQUE = {"type": "string", "pattern": "ev-[0-9a-f]{32}"}


def enum(*values):
    return {"enum": list(values)}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def obj(**properties):
    return {
        "type": "object",
        "required": list(properties),
        "properties": properties,
        "additionalProperties": False,
    }


def array(items, maximum=256):
    return {"type": "array", "maxItems": maximum, "items": items}


def integer(maximum=2**63 - 1):
    return {"type": "integer", "minimum": 0, "maximum": maximum}


BINDING = obj(
    release_id={"type": "string", "pattern": "rc-[0-9a-f]{32}"},
    head=HEX40,
    base=HEX40,
    images=obj(
        **{
            k: nullable({"type": "string", "pattern": "sha256:[0-9a-f]{64}"})
            for k in ("backend", "frontend", "postgres")
        }
    ),
    schema=nullable({"type": "string", "pattern": "[a-z0-9_]{1,64}"}),
    modes=obj(
        **{
            key: (
                {"type": "boolean"}
                if type(value) is bool
                else enum(value, "required")
                if key in {"smtp", "clamav", "signature_update"}
                else enum(value)
            )
            for key, value in MODES.items()
        }
    ),
    content_sha256=nullable(HEX64),
    enabled_features=array(enum(*FEATURES), len(FEATURES)),
)
CHECKLIST_SCHEMA = obj(
    format=enum("MVP050_CHECKLIST/v1"),
    classification=enum("SYNTHETIC ONLY", "REAL UNVERIFIED"),
    pilot=enum("FULL_MVP", "REDUCED_PILOT", "SYNTHETIC_DEMO"),
    binding=BINDING,
    checks=array(
        obj(id=enum(*CATALOG), status=enum(*STATES), evidence=nullable(OPAQUE)),
        len(CATALOG),
    ),
    evidence=array(
        obj(
            id=OPAQUE,
            requirement=enum(*CATALOG, "SCOPE_DECISION"),
            scope=enum(*SCOPES),
            binding_sha256=HEX64,
            artifact=OPAQUE,
        )
    ),
    artifacts=array(
        obj(
            id=OPAQUE,
            filename={"type": "string", "pattern": "[A-Za-z0-9][A-Za-z0-9_.-]{0,119}"},
            sha256=HEX64,
            kind=enum("opaque", "ci", "load"),
        )
    ),
    scope=obj(
        decision=nullable(OPAQUE),
        exclusions=array(
            obj(
                feature=enum(*FEATURES),
                disabled_endpoints=array(
                    enum(*(e for es in ENDPOINTS.values() for e in es)), 30
                ),
                checks=array(enum(*CATALOG), len(CATALOG)),
            ),
            len(FEATURES),
        ),
    ),
    defects=obj(
        review=nullable(OPAQUE),
        entries=array(
            obj(
                id=OPAQUE,
                severity=enum("P0", "P1", "P2"),
                state=enum("OPEN", "CLOSED"),
                feature=enum("core", *FEATURES),
            )
        ),
    ),
)
CI_SCHEMA = obj(
    format=enum("MVP050_CI_SNAPSHOT/v1"),
    head=HEX40,
    binding_sha256=HEX64,
    repository=enum("h1roshigoto/akbrizcase"),
    workflow=enum(".github/workflows/ci.yml"),
    event=enum("push", "pull_request", "workflow_dispatch"),
    branch=enum("main"),
    run_id=integer(),
    attempt=integer(1000),
    status=enum("completed", "in_progress", "queued"),
    conclusion=enum("success", "failure", "cancelled", "skipped", "unknown"),
    jobs=array(
        obj(
            name=enum(*JOBS),
            status=enum("completed", "in_progress", "queued"),
            conclusion=enum("success", "failure", "cancelled", "skipped", "unknown"),
            failures_masked={"type": "boolean"},
        ),
        len(JOBS),
    ),
    inventories=array(
        obj(
            suite=enum(*SUITES),
            collected_sha256=HEX64,
            executed_sha256=HEX64,
            expected=integer(),
            passed=integer(),
            skipped=integer(),
            failed=integer(),
            xfailed=integer(),
            errors=integer(),
        ),
        len(SUITES),
    ),
)


def baseline(head, base, pilot="FULL_MVP"):
    """Unknown values are explicit nulls; this manufactures no events/approvals."""
    modes = copy.deepcopy(MODES)
    # Describe the desired full feature flags, without adding runtime support.
    if pilot != "SYNTHETIC_DEMO":
        modes.update(
            orders=True,
            chat=True,
            knowledge=True,
            files=True,
            real_pii=True,
            smtp="required",
            clamav="required",
            signature_update="required",
        )
    return {
        "format": "MVP050_CHECKLIST/v1",
        "classification": "SYNTHETIC ONLY",
        "pilot": pilot,
        "binding": {
            "release_id": "rc-" + "0" * 32,
            "head": head,
            "base": base,
            "images": dict.fromkeys(("backend", "frontend", "postgres")),
            "schema": SCHEMA,
            "modes": modes,
            "content_sha256": None,
            "enabled_features": list(FEATURES),
        },
        "checks": [
            {
                "id": r["id"],
                "status": "NOT RUN" if r["kind"] == "test" else "BLOCKED",
                "evidence": None,
            }
            for r in REQUIREMENTS
        ],
        "evidence": [],
        "artifacts": [],
        "scope": {"decision": None, "exclusions": []},
        "defects": {"review": None, "entries": []},
    }


def ci_assessment(value, binding):
    validate(value, CI_SCHEMA)
    if value["head"] != binding["head"] or value["binding_sha256"] != digest(binding):
        return "FAIL", "CI_BINDING_MISMATCH"
    jobs = indexed(value["jobs"], "name")
    inventories = indexed(value["inventories"], "suite")
    if (
        value["event"] != "push"
        or value["status"] != "completed"
        or value["conclusion"] != "success"
        or value["run_id"] < 1
        or value["attempt"] < 1
        or set(jobs) != set(JOBS)
        or set(inventories) != set(SUITES)
    ):
        return "INCONCLUSIVE", "CI_PARTIAL_OR_WRONG_ORIGIN"
    if any(
        j["status"] != "completed"
        or j["conclusion"] != "success"
        or j["failures_masked"]
        for j in jobs.values()
    ):
        return "FAIL", "CI_JOB_NOT_SUCCESS"
    for i in inventories.values():
        if (
            i["failed"]
            or i["errors"]
            or i["xfailed"]
            or i["collected_sha256"] != i["executed_sha256"]
            or i["expected"] != i["passed"] + i["skipped"]
            or i["passed"] < 1
        ):
            return "FAIL", "CI_INVENTORY_MISMATCH"
        if i["skipped"]:
            return "INCONCLUSIVE", "CI_SKIPS_REQUIRE_REVIEW"
    return "INCONCLUSIVE", "UNVERIFIED_CI_SNAPSHOT"


def evidence_assessment(evidence, artifacts, records, binding, requirement):
    if evidence["binding_sha256"] != digest(binding):
        return "FAIL", "EVIDENCE_BINDING_MISMATCH"
    if evidence["requirement"] != requirement:
        return "FAIL", "EVIDENCE_REQUIREMENT_MISMATCH"
    artifact = artifacts[evidence["artifact"]]
    if requirement == "A-CI" and artifact["kind"] != "ci":
        return "INCONCLUSIVE", "CI_SNAPSHOT_MISSING"
    if requirement == "C-TARGET-LOAD" and artifact["kind"] != "load":
        return "INCONCLUSIVE", "TARGET_REPORT_MISSING"
    if artifact["kind"] == "ci":
        if evidence["scope"] != "CI_SYNTHETIC" or requirement != "A-CI":
            return "FAIL", "EVIDENCE_SCOPE_MISMATCH"
        return ci_assessment(parse(records[artifact["id"]]), binding)
    if artifact["kind"] == "load":
        if requirement != "C-TARGET-LOAD":
            return "FAIL", "EVIDENCE_REQUIREMENT_MISMATCH"
        report = validate_load(
            parse(records[artifact["id"]]), expected_head=binding["head"]
        )
        identities = report["identities"]
        if (
            any(
                identities[k] != binding["images"][k]
                for k in ("backend", "frontend", "postgres")
            )
            or identities["schema"] != binding["schema"]
        ):
            return "FAIL", "LOAD_BINDING_MISMATCH"
        if (
            evidence["scope"] != "TARGET"
            or report["environment"] != "authorized_target"
        ):
            return "INCONCLUSIVE", "SYNTHETIC_CANNOT_CLOSE_TARGET"
        if report["gates"]["TARGET_LOAD"] != "PASS":
            return report["gates"]["TARGET_LOAD"], "TARGET_LOAD_NOT_PASSED"
        return "INCONCLUSIVE", "UNVERIFIED_TARGET_SNAPSHOT"
    if evidence["scope"] in {"CI_SYNTHETIC", "LOCAL_SYNTHETIC"}:
        return "INCONCLUSIVE", "SYNTHETIC_CANNOT_CLOSE_REAL_GATE"
    return "INCONCLUSIVE", "UNVERIFIED_EXTERNAL_EVIDENCE"


def defect_constraint(defects):
    """Scope declarations cannot establish that a critical defect is harmless."""
    if any(
        d["severity"] in {"P0", "P1"} and d["state"] == "OPEN"
        for d in defects["entries"]
    ):
        return "FAIL", "CRITICAL_DEFECT_PRESENT"
    if defects["review"] is None:
        return "BLOCKED", "DEFECT_TRIAGE_UNVERIFIED"
    return None


def assess(manifest, artifacts, records):
    """PASS describes packet validation only. No caller-provided trust switch."""
    validate(manifest, CHECKLIST_SCHEMA)
    binding = manifest["binding"]
    enabled = binding["enabled_features"]
    require(len(enabled) == len(set(enabled)) and "catalog" in enabled, "SCOPE_INVALID")
    checks = indexed(manifest["checks"], "id")
    evidence = indexed(manifest["evidence"], "id")
    require(set(checks) == set(CATALOG), "CHECKLIST_INCOMPLETE")
    require(
        set(artifacts) == {a["id"] for a in manifest["artifacts"]}, "ARTIFACTS_INVALID"
    )
    require(set(records) == set(artifacts), "ARTIFACTS_INVALID")
    require(
        len({a["filename"] for a in artifacts.values()}) == len(artifacts),
        "DUPLICATE_ID",
    )
    for e in evidence.values():
        require(e["artifact"] in artifacts, "EVIDENCE_MISSING")
        # Even unused evidence must not hide stale bindings or malformed reports.
        require(e["binding_sha256"] == digest(binding), "EVIDENCE_BINDING_MISMATCH")
        if artifacts[e["artifact"]]["kind"] == "ci":
            validate(parse(records[e["artifact"]]), CI_SCHEMA)
        if artifacts[e["artifact"]]["kind"] == "load":
            validate_load(parse(records[e["artifact"]]), expected_head=binding["head"])
    # An unused JSON report must not evade validation through an omitted reference.
    require(
        {e["artifact"] for e in evidence.values()} == set(artifacts),
        "UNREFERENCED_ARTIFACT",
    )
    scope = manifest["scope"]
    exclusions = indexed(scope["exclusions"], "feature")
    missing = set(FEATURES) - set(enabled)
    if manifest["pilot"] == "FULL_MVP":
        require(
            not missing and not exclusions and scope["decision"] is None,
            "SCOPE_INVALID",
        )
    elif manifest["pilot"] == "REDUCED_PILOT":
        require(
            bool(missing)
            and set(exclusions) == missing
            and scope["decision"] in evidence,
            "SCOPE_APPROVAL_MISSING",
        )
    else:
        require(not binding["modes"]["real_pii"], "SYNTHETIC_ESCALATION")
        require(not exclusions and not missing, "SCOPE_INVALID")
    if scope["decision"] is not None:
        decision = evidence[scope["decision"]]
        require(
            decision["requirement"] == "SCOPE_DECISION"
            and decision["scope"] == "EXTERNAL_DECISION"
            and artifacts[decision["artifact"]]["kind"] == "opaque",
            "SCOPE_APPROVAL_MISSING",
        )
    excluded_checks = set()
    for feature, exclusion in exclusions.items():
        require(feature in EXCLUSION_MODES, "UNSUPPORTED_SCOPE_EXCLUSION")
        flag, disabled = EXCLUSION_MODES[feature]
        require(
            type(binding["modes"][flag]) is type(disabled)
            and binding["modes"][flag] == disabled,
            "EXCLUDED_MODE_STILL_ENABLED",
        )
        expected = {
            r["id"]
            for r in REQUIREMENTS
            if r["feature"] == feature and r["id"] not in NON_EXCLUDABLE_REQUIREMENTS
        }
        require(
            set(exclusion["disabled_endpoints"]) == set(ENDPOINTS[feature])
            and len(exclusion["disabled_endpoints"]) == len(ENDPOINTS[feature])
            and set(exclusion["checks"]) == expected
            and len(exclusion["checks"]) == len(expected),
            "UNSAFE_SCOPE_REDUCTION",
        )
        excluded_checks |= expected
    indexed(manifest["defects"]["entries"], "id")
    defect_review = manifest["defects"]["review"]
    if defect_review is not None:
        require(
            defect_review in evidence
            and evidence[defect_review]["requirement"] == "A-DEFECTS",
            "DEFECT_REVIEW_INVALID",
        )
    rows = []
    for requirement in REQUIREMENTS:
        key = requirement["id"]
        check = checks[key]
        reference = check["evidence"]
        if reference is not None:
            require(
                reference in evidence and evidence[reference]["requirement"] == key,
                "EVIDENCE_REQUIREMENT_MISMATCH",
            )
        status, reason = check["status"], "DECLARED_OUTCOME_UNVERIFIED"
        if status == "OUT_OF_SCOPE_APPROVED":
            require(key in excluded_checks, "UNSAFE_SCOPE_REDUCTION")
            status, reason = "INCONCLUSIVE", "SCOPE_DECISION_UNVERIFIED"
        elif key in excluded_checks:
            require(False, "EXCLUSION_MUST_NOT_BE_PASS")
        elif status == "PASS":
            if reference is None:
                status, reason = "INCONCLUSIVE", "PASS_WITHOUT_EVIDENCE"
            else:
                status, reason = evidence_assessment(
                    evidence[reference], artifacts, records, binding, key
                )
        elif status == "BLOCKED":
            reason = "EXTERNAL_PREREQUISITE_MISSING"
        elif status == "NOT RUN":
            reason = "CHECK_NOT_EXECUTED"
        elif status == "FAIL":
            reason = "REPORTED_FAILURE"
        # Missing prerequisites may block a check, but must never weaken a FAIL
        # already declared by the operator or found while checking evidence.
        if (
            status != "FAIL"
            and key == "MODE-SUPPORTED"
            and (
                binding["modes"] != MODES
                or manifest["pilot"] != "SYNTHETIC_DEMO"
                and MODES["data"] == "synthetic-only"
            )
        ):
            status, reason = "BLOCKED", "UNSUPPORTED_RELEASE_MODES"
        if status != "FAIL" and key in {"A-IMAGES", "A-SCHEMA", "D-TARGET"}:
            missing_binding = (
                key == "A-IMAGES"
                and any(v is None for v in binding["images"].values())
                or key == "A-SCHEMA"
                and binding["schema"] != SCHEMA
                or key == "D-TARGET"
                and binding["content_sha256"] is None
            )
            if missing_binding:
                status, reason = "BLOCKED", "RELEASE_BINDING_INCOMPLETE"
        if status != "FAIL" and key == "A-DEFECTS":
            constraint = defect_constraint(manifest["defects"])
            if constraint is not None:
                status, reason = constraint
        if status != "FAIL" and key == "C-TARGET-LOAD" and reference is None:
            status, reason = "BLOCKED", "TARGET_MEASUREMENT_MISSING"
        rows.append(
            {
                "id": key,
                "priority": requirement["priority"],
                "group": requirement["group"],
                "feature": requirement["feature"],
                "included": key in NON_EXCLUDABLE_REQUIREMENTS
                or requirement["feature"] in enabled,
                "status": status,
                "declared_status": check["status"],
                "evidence_present": reference is not None
                or key in excluded_checks
                and scope["decision"] is not None,
                "reason": reason,
                "owner": requirement["owner"],
                "action": requirement["action"],
                "depends_on": requirement["depends_on"],
                "blocks_public_launch": True,
            }
        )
    result = empty_result()
    result.update(
        TECHNICAL_READINESS_CHECK="PASS",
        checklist=rows,
        subject={
            "head": binding["head"],
            "base": binding["base"],
            "binding_sha256": digest(binding),
            "checklist_canonical_sha256": digest(manifest),
        },
        pilot=manifest["pilot"],
        packet_completeness="COMPLETE_FOR_INDEPENDENT_VERIFICATION"
        if all(
            c["status"] in {"PASS", "OUT_OF_SCOPE_APPROVED"}
            and (c["evidence"] is not None or c["id"] in excluded_checks)
            for c in checks.values()
        )
        and all(
            r["reason"]
            in {
                "UNVERIFIED_CI_SNAPSHOT",
                "UNVERIFIED_TARGET_SNAPSHOT",
                "UNVERIFIED_EXTERNAL_EVIDENCE",
                "SCOPE_DECISION_UNVERIFIED",
            }
            for r in rows
        )
        else "INCOMPLETE",
    )
    # Authentication of facts/decisions is deliberately outside this offline tool.
    return result


def empty_result():
    return {
        "format": "MVP050_RESULT/v1",
        "TECHNICAL_READINESS_CHECK": "FAIL",
        "technical_scope": "PACKET_CONSISTENCY",
        "RELEASE_READINESS": "BLOCKED",
        "FULL_MVP_READINESS": "BLOCKED",
        "REDUCED_PILOT_READINESS": "BLOCKED",
        "TARGET_LOAD": "BLOCKED",
        "RECOMMENDATION": "NO-GO",
        "PUBLIC_LAUNCH_RECOMMENDATION": "NO-GO",
        "HUMAN_GO_NO_GO_DECISION": "PENDING",
        "PRODUCTION_ADMISSION": "BLOCKED",
        "packet_completeness": "INCOMPLETE",
        "pilot": None,
        "subject": {},
        "checklist": [],
        "codes": ["OFFLINE_EVIDENCE_IS_NOT_AUTHENTICATED"],
        "historical_baseline": copy.deepcopy(HISTORICAL),
    }


def check_git(repo, head, base):
    # Reuse MVP-049 clean checkout, exact pins and ancestor checks, with no fetch.
    lineage(repo, {"git": {"head": head, "base": base}}, head, base)


def evaluate(packet, checksum, expected_binding, repo, head, base):
    raw = packet.read("checklist.json", checksum, MAX_JSON)
    manifest = parse(raw)
    validate(manifest, CHECKLIST_SCHEMA)
    require(digest(manifest["binding"]) == expected_binding, "RELEASE_BINDING_MISMATCH")
    require(
        manifest["binding"]["head"] == head and manifest["binding"]["base"] == base,
        "LINEAGE_MISMATCH",
    )
    check_git(repo, head, base)
    artifacts = indexed(manifest["artifacts"], "id")
    require(
        "checklist.json" not in {a["filename"] for a in artifacts.values()},
        "ARTIFACTS_INVALID",
    )
    records = {
        key: packet.read(a["filename"], a["sha256"], MAX_JSON)
        for key, a in artifacts.items()
    }
    result = assess(manifest, artifacts, records)
    packet.finish()
    check_git(repo, head, base)
    return result


def main(argv=None):
    packet = None
    try:
        parser = QuietParser(
            description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
        )
        parser.add_argument("operation", choices=("baseline", "check"))
        parser.add_argument("--repo", default=str(ROOT))
        parser.add_argument("--expected-head", required=True)
        parser.add_argument("--expected-base", required=True)
        parser.add_argument("--input-root")
        parser.add_argument("--expected-sha256")
        parser.add_argument("--expected-binding-sha256")
        args = parser.parse_args(argv)
        if args.operation == "baseline":
            require(
                args.input_root
                is args.expected_sha256
                is args.expected_binding_sha256
                is None,
                "ARGUMENT_INVALID",
            )
            check_git(args.repo, args.expected_head, args.expected_base)
            result = assess(baseline(args.expected_head, args.expected_base), {}, {})
            check_git(args.repo, args.expected_head, args.expected_base)
        else:
            require(
                all(
                    type(v) is str and re.fullmatch("[0-9a-f]{64}", v)
                    for v in (args.expected_sha256, args.expected_binding_sha256)
                ),
                "ARGUMENT_INVALID",
            )
            require(args.input_root is not None, "ARGUMENT_INVALID")
            packet = PrivateInput(args.input_root, args.repo)
            result = evaluate(
                packet,
                args.expected_sha256,
                args.expected_binding_sha256,
                args.repo,
                args.expected_head,
                args.expected_base,
            )
        print("MVP050_RESULT " + canonical(result).decode())
        # Exit 0 means the checklist was validated, never production eligibility.
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
        subprocess.SubprocessError,
    ):
        result = empty_result()
        result["codes"].append("PREFLIGHT_REJECTED")
        print("MVP050_RESULT " + canonical(result).decode())
        return 1
    finally:
        if packet is not None:
            packet.close()


if __name__ == "__main__":
    sys.exit(main())
