"""Timing-only subclasses: delegate all pool capacity/deadline logic to SQLAlchemy."""

from contextvars import ContextVar
from time import monotonic_ns, thread_time_ns
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.pool import ConnectionPoolEntry, QueuePool
from sqlalchemy.util.queue import Queue

from app.core.timing import committing, current, span

_acquiring: ContextVar[bool] = ContextVar("pool_timing_acquiring", default=False)


class TimedQueue(Queue[ConnectionPoolEntry]):
    def get(
        self, block: bool = True, timeout: float | None = None
    ) -> ConnectionPoolEntry:
        with span("pool_queue_ms"):
            return super().get(block, timeout)


class TimedQueuePool(QueuePool):
    _queue_class = TimedQueue

    def _do_get(self) -> ConnectionPoolEntry:
        if _acquiring.get():
            return super()._do_get()
        token = _acquiring.set(True)
        try:
            with span("pool_acquire_ms"):
                return super()._do_get()
        finally:
            trace = current.get()
            if trace is not None:
                trace.pool_get_ended_ns = monotonic_ns()
            _acquiring.reset(token)

    def _create_connection(self) -> ConnectionPoolEntry:
        with span("connect_ms"):
            result = super()._create_connection()
        trace = current.get()
        if trace is not None:
            trace.add("new_connections", 1)
        return result


def observe_engine(engine: Engine) -> None:
    def before(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if current.get() is not None:
            context.perf001_clock = (monotonic_ns(), thread_time_ns())
            context.perf001_lock = (
                "pg_advisory_xact_lock" in statement
                or "FOR UPDATE" in statement
                or "FOR SHARE" in statement
            )

    def finished(context: Any) -> None:
        trace = current.get()
        started = getattr(context, "perf001_clock", None)
        if trace is None or started is None:
            return
        context.perf001_clock = None
        elapsed = (monotonic_ns() - started[0]) / 1_000_000
        trace.add("sql_ms", elapsed)
        trace.add("sql_cpu_ms", (thread_time_ns() - started[1]) / 1_000_000)
        trace.add("sql_calls", 1)
        if committing.get():
            trace.add("commit_sql_ms", elapsed)
        if context.perf001_lock:
            trace.add("lock_statement_ms", elapsed)

    def after(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        finished(context)

    def failed(exception_context: Any) -> None:
        finished(exception_context.execution_context)

    pool = engine.pool
    assert isinstance(pool, QueuePool)

    def checkout(dbapi: Any, record: Any, proxy: Any) -> None:
        trace = current.get()
        if trace is not None:
            now = monotonic_ns()
            if trace.pool_get_ended_ns:
                trace.add(
                    "checkout_after_get_ms", (now - trace.pool_get_ended_ns) / 1_000_000
                )
                trace.pool_get_ended_ns = 0
            record.info["perf001_hold"] = (trace, now)
            trace.add("pool_checkouts", 1)
            trace.metrics["pool_peak_in_use"] = max(
                trace.metrics["pool_peak_in_use"], pool.checkedout()
            )

    def checkin(dbapi: Any, record: Any) -> None:
        held = record.info.pop("perf001_hold", None)
        if held is not None:
            trace, started = held
            trace.add("connection_hold_ms", (monotonic_ns() - started) / 1_000_000)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failed)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
