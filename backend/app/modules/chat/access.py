"""Owner authentication precedes expiry and history lookup; caller owns the UoW."""

import hashlib
import hmac
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_types import utc_now
from app.modules.chat.models import ChatSession


class ChatNotFound(Exception):
    """One public failure for an absent object or insufficient ownership proof."""


class ChatExpired(Exception):
    """Only raised after successful owner authentication."""


def owner_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def require_owner(session: Session, identifier: UUID, token: str | None) -> ChatSession:
    if token is None or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ChatNotFound
    # Shared lock gives GET a consistent state/history while future writers hold
    # their exclusive session lock. Refresh even if this Session was used before.
    row = session.scalar(
        select(ChatSession)
        .where(ChatSession.id == identifier)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    matches = hmac.compare_digest(
        owner_token_hash(token), row.owner_token_hash if row else "0" * 64
    )
    if row is None or not matches:
        raise ChatNotFound
    if row.expires_at <= utc_now():
        raise ChatExpired
    return row
