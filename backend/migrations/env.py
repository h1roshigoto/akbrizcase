"""Explicit online migrations; credentials come only from runtime files."""

from alembic import context

from app.core import staff_models  # noqa: F401 -- register staff metadata
from app.core.db import DatabaseSettings, create_database_engine
from app.core.db_types import Base

if context.is_offline_mode():
    raise RuntimeError("Offline migrations disabled; use the explicit migrate command")

engine = create_database_engine(DatabaseSettings.from_environment(migration=True))
try:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="app",
            include_schemas=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
