"""Post-measurement PG16 investigation; synthetic stack only, rollback every write.

This is not load evidence. It never changes PostgreSQL settings or quotas. Only
closed numeric aggregates leave the process; SQL/parameters/plans stay private.
"""

import cProfile
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import event, select, text

from app.core.db import Database, DatabaseSettings
from app.modules.catalog.models import CatalogItem
from app.modules.chat.messages import create_message, read_message_result
from app.modules.chat.service import create_session
from app.modules.kb.tree_reads import load_tree
from app.modules.orders.models import ConsentText
from app.modules.orders.security import OrderSecurity
from app.modules.orders.service import create_order
from app.schemas.chat import ChatMessageCreate, ChatSessionCreate
from app.schemas.common import OrderHeaders
from app.schemas.orders import OrderCreate


FUNCTIONS = {
    "create_order",
    "effective_consent",
    "approved_publication",
    "lock_privacy",
    "create_message",
    "read_message_result",
    "load_tree",
    "restore_state",
    "get_source",
    "validate",
    "route",
    "project",
}


def profile_call(database, operation):
    queries = []

    def before(conn, cursor, statement, parameters, context, executemany):
        context.perf001_started = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        queries.append(
            (
                statement,
                parameters,
                (time.perf_counter() - context.perf001_started) * 1000,
            )
        )

    event.listen(database.engine, "before_cursor_execute", before)
    event.listen(database.engine, "after_cursor_execute", after)
    profiler = cProfile.Profile()
    started, cpu = time.perf_counter(), time.thread_time()
    try:
        profiler.enable()
        operation()
    finally:
        profiler.disable()
        event.remove(database.engine, "before_cursor_execute", before)
        event.remove(database.engine, "after_cursor_execute", after)
    result = {
        "wall_ms": (time.perf_counter() - started) * 1000,
        "thread_cpu_ms": (time.thread_time() - cpu) * 1000,
        "sql_ms": sum(q[2] for q in queries),
        "sql_count": len(queries),
        "functions_ms": {},
    }
    for entry in profiler.getstats():
        code = entry.code
        if not isinstance(code, str) and code.co_name in FUNCTIONS:
            key = code.co_name
            result["functions_ms"][key] = (
                result["functions_ms"].get(key, 0) + entry.totaltime * 1000
            )
    # Explain only SELECTs already issued by the tree reader. Never explain DML.
    plans = []
    for statement, parameters, _ in queries:
        if statement.startswith("SELECT") and "diag_trees" in statement:
            with database.engine.connect() as connection:
                plan = connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
                ).scalar_one()[0]
                plans.append(
                    {
                        "planning_ms": plan["Planning Time"],
                        "execution_ms": plan["Execution Time"],
                        "cost": plan["Plan"]["Total Cost"],
                        "jit_ms": plan.get("JIT", {}).get("Timing", {}).get("Total", 0),
                        "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
                        "shared_read_blocks": plan["Plan"].get("Shared Read Blocks", 0),
                    }
                )
    result["tree_plans"] = plans
    return result


def main():
    database = Database(DatabaseSettings.from_environment())
    try:
        with database.unit_of_work() as session:
            assert os.environ.get("APP_ENV") == "demo"
            assert (
                session.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                        "WHERE datname=current_database()"
                    )
                )
                == "akbrizcase:synthetic-seed:demo"
            )
            assert session.scalar(text("SHOW server_version_num")).startswith("16")
            item = session.scalars(
                select(CatalogItem).where(CatalogItem.sku == "SYNTHETIC035-LIMIT-00")
            ).one()
            consent = session.scalars(select(ConsentText)).one().version
            pin = session.scalar(
                text(
                    "SELECT id FROM app.diag_trees WHERE key='SYNTHETIC035' "
                    "AND status='published'"
                )
            )
            assert pin is not None
            baseline = graph_counts(session)
        order = OrderCreate.model_validate(
            {
                "kind": "commercial",
                "source": "site",
                "items": [
                    {"item_id": item.id, "qty": 1, "expected_revision": item.revision}
                ],
                "contact": {
                    "name": "SYNTHETIC PERF001 ONLY",
                    "email": "perf001@example.invalid",
                },
                "consent": {"version": consent, "accepted": True},
                "confirmed": True,
            }
        )
        security = OrderSecurity(b"SYNTHETIC-PERF001-ONLY-ROLLBACK-KEY")

        def rollback_order():
            with database.sessions() as session:
                try:
                    create_order(
                        session,
                        order,
                        OrderHeaders(
                            **{"Idempotency-Key": uuid4(), "X-Request-Token": "a" * 64}
                        ),
                        security,
                    )
                finally:
                    session.rollback()

        def rollback_button():
            with database.sessions() as session:
                try:
                    own = create_session(session, ChatSessionCreate(mode="diagnose"))
                    create_message(
                        session,
                        ChatMessageCreate(
                            session_id=own.session_id,
                            request_id=uuid4(),
                            expected_version=0,
                            input={
                                "type": "start_diagnosis",
                                "model_id": pin_model,
                                "tree_key": "SYNTHETIC035",
                            },
                        ),
                        own.session_token,
                    )
                    identifier = create_message(
                        session,
                        ChatMessageCreate(
                            session_id=own.session_id,
                            request_id=uuid4(),
                            expected_version=1,
                            input={"type": "button", "option_id": "a"},
                        ),
                        own.session_token,
                    )
                    read_message_result(
                        session, own.session_id, identifier, own.session_token
                    )
                finally:
                    session.rollback()

        with database.unit_of_work() as session:
            pin_model = load_tree(session, tree_id=pin).model_id
        result = {
            "schema": "PERF001_PROBE/v1",
            "scope": "POST_MEASUREMENT_ROLLBACK",
            "order": profile_call(database, rollback_order),
            "diagnosis_start_button_projection": profile_call(
                database, rollback_button
            ),
        }
        # Controlled lock experiment: retain one complete order's locks, then
        # observe whether an independent order on the same item can finish.
        entered, release = threading.Event(), threading.Event()

        def holder():
            with database.sessions() as session:
                try:
                    create_order(
                        session,
                        order,
                        OrderHeaders(
                            **{"Idempotency-Key": uuid4(), "X-Request-Token": "b" * 64}
                        ),
                        security,
                    )
                    entered.set()
                    assert release.wait(2)
                finally:
                    session.rollback()

        with ThreadPoolExecutor(max_workers=2) as executor:
            held = executor.submit(holder)
            assert entered.wait(3)
            started = time.perf_counter()
            competing = executor.submit(rollback_order)
            blocked = False
            try:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline and not competing.done():
                    with database.unit_of_work() as session:
                        blocked |= bool(
                            session.scalar(
                                text(
                                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                    "WHERE datname=current_database() AND "
                                    "usename=current_user AND wait_event_type='Lock' "
                                    "AND cardinality(pg_blocking_pids(pid))>0)"
                                )
                            )
                        )
                    time.sleep(0.01)
                completed_while_held = competing.done()
            finally:
                release.set()
            held.result()
            competing.result()
            result["same_item_contention"] = {
                "competing_wall_ms": (time.perf_counter() - started) * 1000,
                "pg_lock_wait_observed": blocked,
                "completed_while_held": completed_while_held,
            }
        with database.unit_of_work() as session:
            assert graph_counts(session) == baseline
        result["no_side_effects"] = True
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    finally:
        database.close()


def graph_counts(session):
    return [
        session.scalar(text("SELECT count(*) FROM app." + table))
        for table in (
            "orders",
            "consents",
            "order_items",
            "order_status_history",
            "email_outbox",
            "chat_sessions",
            "chat_messages",
            "rate_limit_buckets",
        )
    ]


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never disclose SQL, parameters, DTO inputs or exception text.
        print("PERF001_PROBE_FAILED", file=__import__("sys").stderr)
        raise SystemExit(1) from None
