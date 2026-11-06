"""Private PG graph assertions and scoped failure seams for MVP-044.

No business implementation lives here. Never include private rows in assertion
diagnostics: pytest's ordinary diff rendering could expose verification material.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection

from app.core.db import Database
from app.modules.orders.erasure_service import PII_FIELDS

TABLES = (
    "orders",
    "order_items",
    "consents",
    "order_status_history",
    "email_outbox",
    "chat_sessions",
    "chat_messages",
    "audit_events",
    "deletion_journal",
    "rate_limit_buckets",
)


def same(actual: object, expected: object, boundary: str = "graph") -> None:
    __tracebackhide__ = True
    if actual != expected:
        pytest.fail(f"Private {boundary} comparison failed (values suppressed)")


@dataclass(repr=False)
class Graph:
    rows: dict[str, list[dict[str, Any]]] = field(repr=False)

    def order(self, identifier: UUID) -> dict[str, Any]:
        return next(row for row in self.rows["orders"] if row["id"] == str(identifier))

    def linked(self, identifier: UUID, table: str) -> list[dict[str, Any]]:
        order = self.order(identifier)
        if table == "orders":
            return [order]
        if table == "consents":
            return [row for row in self.rows[table] if row["id"] == order["consent_id"]]
        return [row for row in self.rows[table] if row["order_id"] == str(identifier)]

    def business(self, identifier: UUID, *, outbox: bool = True) -> dict[str, object]:
        return {
            table: self.linked(identifier, table)
            for table in TABLES[:5]
            if outbox or table != "email_outbox"
        }

    def audits(self, action: str, identifier: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows["audit_events"]
            if row["action"] == action and row["entity_id"] == identifier
        ]


def graph(database: Database) -> Graph:
    # One snapshot for all tables, independent from the operation's UoW.
    with database.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        result = Graph(
            {
                table: list(
                    connection.scalars(
                        text(
                            f"SELECT row_to_json(t) FROM app.{table} t "
                            "ORDER BY to_jsonb(t)::text"
                        )
                    )
                )
                for table in TABLES
            }
        )
    for order in result.rows["orders"]:
        identifier = UUID(order["id"])
        items = result.linked(identifier, "order_items")
        consents = result.linked(identifier, "consents")
        history = result.linked(identifier, "order_status_history")
        notifications = result.linked(identifier, "email_outbox")
        same(len(consents), 1, "consent cardinality")
        same(
            sum(
                row["from_status"] is None and row["to_status"] == "new"
                for row in history
            ),
            1,
            "initial history",
        )
        same(len(history), order["version"], "history/version")
        same(len(items) == 0, order["kind"] == "support", "item kind")
        same(len(notifications) <= 1, True, "outbox uniqueness")
        known = sum(
            (
                Decimal(str(row["unit_price_snapshot"])) * row["qty"]
                for row in items
                if row["unit_price_snapshot"] is not None
            ),
            Decimal(0),
        )
        same(Decimal(str(order["known_subtotal"])), known, "known subtotal")
        unknown = not items or any(
            row["price_mode_snapshot"] == "on_request" for row in items
        )
        same(order["items_total"] is None, unknown, "unknown items total")
        shipping = any(
            row["kind_snapshot"] == "product" and row["requires_shipping_snapshot"]
            for row in items
        )
        same(order["delivery_status"], "pending_quote" if shipping else "not_required")
        same(order["delivery_cost"] is None, shipping, "unknown delivery")
        if not shipping:
            same(Decimal(str(order["delivery_cost"])), Decimal(0), "service delivery")
        same(order["total_amount"] is None, unknown or shipping, "unknown total")
        if order["pii_erased_at"] is not None:
            same(all(order[name] is None for name in PII_FIELDS), True, "erased fields")
            same(all(row["reason"] is None for row in history), True, "erased reasons")
        if order["chat_session_id"] is not None:
            same(
                any(
                    row["id"] == order["chat_session_id"]
                    for row in result.rows["chat_sessions"]
                ),
                True,
                "chat FK",
            )
    return result


@contextmanager
def pg_fault(
    database: Database, table: str, *, verb: str = "INSERT INTO", commit: bool = False
) -> Iterator[list[bool]]:
    """Fail after a real write (or its commit), using a real PG division error."""
    reached = [False]
    armed: set[Connection] = set()

    def written(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        if statement.lstrip().startswith(f"{verb} app.{table}"):
            armed.add(connection)
            if not commit:
                reached[0] = True
                connection.exec_driver_sql("SELECT 1/0")

    def committing(connection: Connection) -> None:
        if connection in armed:
            reached[0] = True
            connection.exec_driver_sql("SELECT 1/0")

    event.listen(database.engine, "after_cursor_execute", written)
    if commit:
        event.listen(database.engine, "commit", committing)
    try:
        yield reached
    finally:
        event.remove(database.engine, "after_cursor_execute", written)
        if commit:
            event.remove(database.engine, "commit", committing)
    same(reached[0], True, "fault seam reached")


def wait_for_blocker(database: Database, blocker: int) -> None:
    """Observe an actual waiting backend; never use sleep as race evidence."""
    deadline = monotonic() + 2
    while monotonic() < deadline:
        with database.engine.connect() as connection:
            if connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks "
                    "WHERE NOT granted AND :pid = ANY(pg_blocking_pids(pid)))"
                ),
                {"pid": blocker},
            ):
                return
    pytest.fail("Expected independent PostgreSQL lock waiter was not observed")
