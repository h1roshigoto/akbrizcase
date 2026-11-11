"""Closed numeric-only request timing schema; never SQL, paths or payloads."""

import math

CLOCKS = frozenset(
    {
        "asgi_start_ns",
        "process_start_cpu_ns",
        "process_end_cpu_ns",
        "handler_start_ns",
        "route_start_ns",
        "response_start_ns",
        "response_end_ns",
    }
)
COUNTS = frozenset(
    {
        "argon2_calls",
        "worker_calls",
        "thread_tokens_before",
        "thread_tokens_total",
        "pool_checkouts",
        "new_connections",
        "pool_peak_in_use",
        "sql_calls",
        "commit_calls",
        "rollback_calls",
    }
)
DURATIONS = frozenset(
    {
        "argon2_ms",
        "argon2_process_cpu_ms",
        "dispatch_ms",
        "worker_ms",
        "worker_cpu_ms",
        "pool_acquire_ms",
        "pool_queue_ms",
        "connect_ms",
        "commit_sql_ms",
        "commit_pool_ms",
        "checkout_after_get_ms",
        "connection_hold_ms",
        "sql_ms",
        "sql_cpu_ms",
        "lock_statement_ms",
        "commit_ms",
        "rollback_ms",
        "chat_write_ms",
        "chat_projection_ms",
        "projection_commit_ms",
    }
)
METRICS = CLOCKS | COUNTS | DURATIONS


def checked_timings(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict) or set(value) != METRICS:
        raise ValueError("TIMING_SCHEMA_INVALID")
    for name, number in value.items():
        if name in CLOCKS | COUNTS:
            if type(number) is not int or not 0 <= number <= 2**63 - 1:
                raise ValueError("TIMING_SCHEMA_INVALID")
        elif (
            type(number) not in {int, float}
            or not math.isfinite(number)
            or not 0 <= number <= 86_400_000
        ):
            raise ValueError("TIMING_SCHEMA_INVALID")
    return value
