"""PostgreSQL infrastructure. Only the outer UoW owns commit/rollback.

Repositories and services receive the same Session and may flush, never commit.
UUID v4, aware UTC timestamps and Decimal money are preserved by db_types.
"""

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.csrf import StaffSecurity
from app.modules.orders.security import OrderSecurity

SCHEMA_REVISION = "0006_mvp027_diagnostic_trees"


class DatabaseConfigurationError(ValueError):
    """Safe configuration failure; never include credentials or file contents."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def from_environment(cls, *, migration: bool = False) -> "DatabaseSettings":
        prefix = "MIGRATION_DB" if migration else "DB"
        try:
            password = Path(os.environ[f"{prefix}_PASSWORD_FILE"]).read_text().strip()
            if not password:
                raise ValueError
            port = int(os.environ.get("DB_PORT", "5432"))
            if not 1 <= port <= 65535:
                raise ValueError
            return cls(
                host=os.environ.get("DB_HOST", "postgres"),
                port=port,
                database=os.environ.get("DB_NAME", "akbrizcase"),
                user=os.environ.get(
                    f"{prefix}_USER",
                    "akbrizcase_migrator" if migration else "akbrizcase_runtime",
                ),
                password=password,
            )
        except (KeyError, OSError, ValueError):
            raise DatabaseConfigurationError(
                "Database configuration unavailable"
            ) from None

    def url(self) -> URL:
        # URL.create handles reserved characters without string interpolation.
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )


def create_database_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(
        settings.url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=3,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 3,
            "options": "-c timezone=UTC -c statement_timeout=5000 "
            "-c lock_timeout=3000 -c idle_in_transaction_session_timeout=15000 "
            "-c search_path=app,public",
        },
    )


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.engine = create_database_engine(settings)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def unit_of_work(self) -> Iterator[Session]:
        """One Session/transaction shared by all operations; commit on success.

        An exception, including a failed commit or cancellation, rolls back.
        The session always closes and returns its connection to the pool.
        """
        with self.sessions() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                version = int(connection.scalar(text("SHOW server_version_num")))
                revisions = set(
                    connection.scalars(
                        text("SELECT version_num FROM app.alembic_version")
                    )
                )
                # Fail closed for privileged credentials, even if misconfigured.
                safe_role = connection.scalar(
                    text(
                        "SELECT NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole "
                        "OR r.rolreplication OR r.rolbypassrls "
                        "OR pg_has_role(current_user, d.datdba, 'MEMBER') "
                        "OR pg_has_role(current_user, n.nspowner, 'MEMBER') "
                        "OR has_schema_privilege(current_user, 'app', 'CREATE')) "
                        "FROM pg_roles r CROSS JOIN pg_database d "
                        "CROSS JOIN pg_namespace n "
                        "WHERE r.rolname = current_user "
                        "AND d.datname = current_database() AND n.nspname = 'app'"
                    )
                )
                return (
                    160000 <= version < 170000
                    and revisions == {SCHEMA_REVISION}
                    and safe_role is True
                )
        except (SQLAlchemyError, OSError):
            # DB errors may contain DSNs, hostnames or SQL. Do not expose/log them.
            return False

    def close(self) -> None:
        self.engine.dispose()


@asynccontextmanager
async def database_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure lazily, without connecting or migrating on API startup."""
    database = None
    try:
        database = Database(DatabaseSettings.from_environment())
    except DatabaseConfigurationError:
        pass
    app.state.database = database
    app.state.staff_security = StaffSecurity.from_environment()
    app.state.order_security = OrderSecurity.from_environment()
    try:
        yield
    finally:
        if database is not None:
            database.close()
