"""Fail closed unless every MVP-044 case actually passed; emit safe evidence."""

import json
import re

from compare_ci_tests import ANSI

PREFIX = "tests/integration/test_commercial_regression.py::"
EXPECTED = {
    "test_linked_lifecycle_catalog_status_smtp_retry_privacy": 3,
    "test_creation_fault_rolls_back_then_same_key_completes_lifecycle": 6,
    "test_creation_lost_commit_ack_reconciles_original_receipt": 1,
    "test_status_fault_after_smtp_preserves_whole_graph": 3,
    "test_manual_retry_fault_after_catalog_and_status": 2,
    "test_support_chat_cleanup_fault_manager_read_and_later_erasure": 3,
    "test_parallel_post_unique_wait_then_status_and_worker": 6,
    "test_distinct_parallel_keys_unique_numbers_and_quota": 3,
    "test_creation_contends_with_catalog_or_consent": 12,
    "test_replay_contends_with_witnessed_erasure": 6,
    "test_two_staff_versions_on_created_graph_and_disabled_session": 3,
    "test_worker_fault_after_status_and_catalog_preserves_graph": 5,
    "test_two_workers_lease_cas_and_protected_retry_states": 3,
    "test_journal_faults_after_http_status_and_smtp": 4,
    "test_historical_policy_no_latest_fallback_and_full_graph_fk": 2,
    "test_aq01_transport_and_hidden_matrix_after_cross_module_changes": 2,
}


def acceptance(output: str, head: str, *, dirty: bool = False) -> dict[str, object]:
    output = ANSI.sub("", output)
    total = sum(EXPECTED.values())
    records = re.findall(
        r"^(tests/\S+)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b",
        output,
        re.MULTILINE,
    )
    groups: dict[str, int] = {}
    for node, outcome in records:
        if outcome != "PASSED" or not node.startswith(PREFIX):
            raise ValueError("MVP-044 requires every selected case to pass")
        name = node.removeprefix(PREFIX).split("[", 1)[0]
        groups[name] = groups.get(name, 0) + 1
    if (
        not re.fullmatch(r"[0-9a-f]{40}", head)
        or len({node for node, _ in records}) != total
        or groups != EXPECTED
        or re.findall(r"collected (\d+) items", output) != [str(total)]
        or re.findall(r"\b(\d+) passed(?:,| in)", output) != [str(total)]
        or re.search(
            r"\b(?:skipped|deselected|xfailed|xpassed|failed|errors?)\b",
            output,
            re.IGNORECASE,
        )
    ):
        raise ValueError("MVP-044 execution is incomplete; acceptance denied")
    return {
        "head": head,
        "worktree_dirty": dirty,
        "groups": groups,
        "passed": total,
        "result": "PASS",
    }


def emit(output: str, head: str, *, dirty: bool = False) -> None:
    print(
        "COMMERCIAL_REGRESSION "
        + json.dumps(acceptance(output, head, dirty=dirty), sort_keys=True),
        flush=True,
    )
