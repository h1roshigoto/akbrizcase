"""Shared SQL types/conventions; no domain tables are declared here."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, MetaData, Numeric, Uuid
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    metadata = MetaData(
        schema="app",
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )
    type_annotation_map = {
        UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
        Decimal: Numeric(12, 2, asdecimal=True),
    }


def utc_now() -> datetime:
    return datetime.now(UTC)
