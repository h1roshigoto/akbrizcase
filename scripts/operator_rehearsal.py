"""MVP-043 synthetic operator rehearsal; never a production or human acceptance."""

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from db_integration import run

REPO = Path(__file__).resolve().parents[1]
INTEGRATION = "tests/integration/"


def nodes(module, *names):
    return tuple(INTEGRATION + module + ".py::" + name for name in names)


# Reuse the real business/HTTP/ClamAV/PTY assertions, with fresh DBs where the
# legacy staff teardown cannot delete immutable historical reviewer references.
STAGES = {
    "staff": nodes(
        "test_operator_handover", "test_hidden_staff_cli_and_disabled_sessions"
    )
    + nodes("test_staff", "test_empty_bootstrap_and_repeat"),
    "knowledge": nodes(
        "test_kb_import", "test_real_import_check_noop", "test_auth_before_file_access"
    )
    + nodes(
        "test_kb_lifecycle",
        "test_lifecycle_actual_cli_and_commit_failure",
        "test_lifecycle_admin_only",
        "test_lifecycle_real_tcp_http_visibility_retire_revoke",
    ),
    "trees": nodes(
        "test_diagnostic_trees",
        "test_import_check_normalization_audit_and_no_side_effects",
        "test_authorization_before_file_access",
    )
    + nodes(
        "test_tree_lifecycle",
        "test_publish_replace_restore_and_exact_pins",
        "test_expert_evidence_is_required_and_bound",
        "test_document_preview_and_revoke_guard",
        "test_authorization_before_io",
        "test_real_cli_publication_and_revoke",
    ),
    "images": nodes(
        "test_admin_images",
        "test_admin_install_replace_remove",
        "test_upload_rejects_untrusted_forms",
        "test_new_direct_routes_require_admin",
    )
    + nodes("test_catalog_image_binding", "test_nonpublic_missing_and_invalid_http"),
    "smtp": nodes(
        "test_operator_smtp", "test_real_failure_manager_retry_without_new_order"
    )
    + nodes("test_notification_retry", "test_state_matrix", "test_security")
    + nodes("test_email_worker", "test_crash_before_and_after_smtp"),
    "privacy": nodes(
        "test_cleanup_review",
        "test_legacy_manual_authority_denials",
        "test_legacy_manual_requires_explicit_valid_journal_packet",
        "test_automatic_policy_still_rejects_wrong_chronology_or_packet",
    )
    + nodes(
        "test_deletion_reconciliation",
        "test_crash_export_and_exact_redelivery",
        "test_mutation_commit_failure_and_lost_ack_reconciliation",
        "test_sink_failure_never_claims_completion",
        "test_reconciliation_rejects_local_conflict_and_production",
    ),
    "diagnostics": nodes(
        "test_operator_handover", "test_report_terminal_acl_and_missing_stale_receipts"
    )
    + nodes(
        "test_safe_operations",
        "test_protected_aggregates_and_failure_signals",
        "test_actual_missing_postgres_is_not_success",
    )
    + ("tests/test_observability.py::test_backup_and_disk_failure_signals",),
}


def local_environment(source):
    environment = source.copy()
    for key in list(environment):
        if key.startswith(("COMPOSE_", "DB_", "MIGRATION_DB_", "SMTP_")) or key in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }:
            environment.pop(key)
    # Existing trusted development hook supplies only a BuildKit CA, not data.
    if "DB_TEST_BUILD_OVERRIDE" in source:
        environment["DB_TEST_BUILD_OVERRIDE"] = source["DB_TEST_BUILD_OVERRIDE"]
    # Overrides a saved currentContext as well as ambient remote selectors.
    environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    environment.pop("CI_INVENTORY_DIR", None)
    return environment


def checked_backup(output, head):
    lines = [
        line.removeprefix("BACKUP_EVIDENCE ")
        for line in output.splitlines()
        if line.startswith("BACKUP_EVIDENCE ")
    ]
    if len(lines) != 1:
        raise ValueError("Missing backup evidence")
    evidence = json.loads(lines[0])
    if (
        evidence.get("head") != head
        or evidence.get("offsite") != "BLOCKED"
        or evidence.get("target_rpo_rto") != "BLOCKED"
        or evidence.get("new_order_admission") is not False
        or evidence.get("smtp_disabled") is not True
        or evidence.get("post_snapshot_number_protected") is not True
        or evidence.get("incomplete_restore_blocked") is not True
        or evidence.get("sequence_negative_cases", 0) < 15
    ):
        raise ValueError("Invalid backup evidence")
    return evidence


def main(*, pilot_content=False):
    environment = local_environment(os.environ)
    # No caller-supplied project, endpoints, credentials, stages or success flags.
    with tempfile.TemporaryDirectory(prefix="akbriz-operator-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        stage = "preflight"
        try:
            if Path.cwd().resolve() != REPO:
                raise ValueError("Run from the repository root")
            if subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout:
                raise ValueError("Clean checkout required")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout.strip()
            if pilot_content and environment.get("MVP049_EXPECTED_HEAD", head) != head:
                raise ValueError("Unexpected content rehearsal HEAD")
            evidence = {
                "head": head,
                "stages": {},
                "production": "BLOCKED",
                "human_handover": "BLOCKED",
            }
            stages = dict(STAGES)
            counts = {}
            fts = None
            if pilot_content:
                from mvp049_rehearsal import PILOT_STAGES

                stages.update(PILOT_STAGES)
            for index, (stage, paths) in enumerate(stages.items()):
                print("OPERATOR_STAGE_START " + stage, flush=True)
                log = root / (stage + ".log")
                with (
                    log.open("x") as stream,
                    contextlib.redirect_stdout(stream),
                    contextlib.redirect_stderr(stream),
                ):
                    log.chmod(0o600)
                    run(
                        "akbrizcase-dbtest-" + uuid.uuid4().hex[:12],
                        suite="kb-acceptance"
                        if stage == "pilot-search"
                        else "operator-" + stage,
                        reuse_image=index != 0,
                        test_paths=paths,
                        command_timeout=600,
                        test_mark="integration or not integration",
                        environment=environment,
                        capture_all=True,
                    )
                # Fail if no tests, skipped requirements or unexpected xfails:
                # pytest exit 0 alone must not manufacture smoke acceptance.
                summaries = re.findall(
                    r"=+ ([^\n]*\d+ passed[^\n]*) =+", log.read_text()
                )
                if len(summaries) != 1 or re.search(
                    r"\b(skipped|xfailed|xpassed|failed|error)\b", summaries[0]
                ):
                    raise ValueError("Incomplete operator stage")
                if pilot_content:
                    from mvp049_rehearsal import passed_count

                    counts[stage] = passed_count(log.read_text())
                    if stage == "pilot-search":
                        from kb_acceptance_result import strict_json, validate

                        records = re.findall(
                            r"KB_ACCEPTANCE_RESULT (.+)", log.read_text()
                        )
                        if len(records) != 1:
                            raise ValueError("Incomplete FTS evidence")
                        measured = validate(strict_json(records[0]), head, False)
                        fts = {
                            "corpus_sha256": measured["corpus_sha256"],
                            "relevant": measured["relevant_count"],
                            "negative": measured["negative_count"],
                            "hits": measured["hits"],
                            "negative_safe": measured["negative_passed"],
                            "acl_incidents": measured["acl_incidents"],
                        }
                evidence["stages"][stage] = "PASS"
                print("OPERATOR_STAGE_PASS " + stage, flush=True)
            stage = "backup"
            print("OPERATOR_STAGE_START backup", flush=True)
            result = subprocess.run(
                [sys.executable, "scripts/backup_rehearsal.py"],
                cwd=REPO,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=1200,
            )
            checked_backup(result.stdout, head)
            evidence["stages"][stage] = "PASS"
            print("OPERATOR_STAGE_PASS backup", flush=True)
            content_result = None
            record = None
            if pilot_content:
                from mvp049_rehearsal import check_schema_fixture, receipt

                record = receipt(REPO, head, counts, fts)
                content_result = check_schema_fixture(
                    REPO, head, record, root / "schema-fixture"
                )
                # Only this live control flow has observed every successful
                # stage and checked restore on the exact HEAD. A receipt read
                # from disk (even one emitted here earlier) has no such authority.
                content_result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"] = "PASS"
                content_result["codes"].remove("UNVERIFIED_REHEARSAL_RECEIPT")
                content_result["codes"].append("CURRENT_PROCESS_REHEARSAL")
            print(
                "OPERATOR_EVIDENCE " + json.dumps(evidence, sort_keys=True), flush=True
            )
            if pilot_content:
                from mvp049_content_gate import canonical

                print("MVP049_REHEARSAL " + canonical(record).decode(), flush=True)
                print("MVP049_RESULT " + canonical(content_result).decode(), flush=True)
            return 0
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            # Child logs/secrets/paths/DB exceptions are never emitted or uploaded.
            # The shared harness stops owned containers and retains all volumes.
            print("OPERATOR_STAGE_FAIL " + stage, file=sys.stderr, flush=True)
            if pilot_content:
                from mvp049_content_gate import canonical, empty_result

                failure = empty_result()
                failure["gates"]["SYNTHETIC_CONTENT_REHEARSAL"] = "FAIL"
                failure["codes"] = ["REHEARSAL_FAILED"]
                print("MVP049_RESULT " + canonical(failure).decode(), flush=True)
            return 1


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--pilot-content"]):
        print("OPERATOR_STAGE_FAIL arguments", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(pilot_content=sys.argv[1:] == ["--pilot-content"]))
