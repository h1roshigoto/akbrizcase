"""Server-only viewers and the shared document/version SQL access predicate.

Chat must pass PUBLIC_VIEWER explicitly. HTTP staff reads construct a viewer only
from the existing resolved PostgreSQL session, never request role/actor fields.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.core.staff_models import StaffSession, StaffUser
from app.modules.kb.models import Document, DocumentVersion


@dataclass(frozen=True)
class Viewer:
    staff_session_id: UUID | None = None


PUBLIC_VIEWER = Viewer()


def staff_access(viewer: Viewer) -> ColumnElement[bool]:
    if viewer.staff_session_id is None:
        return false()
    return exists(
        select(StaffSession.id)
        .join(StaffUser, StaffUser.id == StaffSession.user_id)
        .where(
            StaffSession.id == viewer.staff_session_id,
            StaffSession.revoked_at.is_(None),
            # Unlike transaction_timestamp(), this also expires during file scans.
            StaffSession.expires_at > func.clock_timestamp(),
            StaffUser.is_active.is_(True),
            StaffUser.role.in_(("manager", "admin")),
        )
    )


def accessible_version(viewer: Viewer, *, exact: bool = False) -> ColumnElement[bool]:
    status = DocumentVersion.status == "published"
    if exact:
        # A previous edition needs evidence that it was actually approved and
        # published. A draft merely relabelled retired is never a download.
        status = or_(
            status,
            and_(
                DocumentVersion.status == "retired",
                DocumentVersion.reviewed_by.is_not(None),
                DocumentVersion.approved_at.is_not(None),
                DocumentVersion.published_at.is_not(None),
            ),
        )
    return and_(
        DocumentVersion.document_id == Document.id,
        Document.status == "active",
        or_(
            Document.visibility == "public",
            and_(Document.visibility == "internal", staff_access(viewer)),
        ),
        status,
    )
