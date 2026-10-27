"""Server-only viewers and the shared document/version SQL access predicate.

Chat must pass PUBLIC_VIEWER explicitly. HTTP viewers carry only the existing
cookie's server-computed hash; every SQL read verifies the current session/user.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import token_hash, valid_token
from app.modules.kb.models import Document, DocumentVersion


@dataclass(frozen=True)
class Viewer:
    staff_session_id: UUID | None = None
    staff_token_hash: str | None = field(default=None, repr=False)


PUBLIC_VIEWER = Viewer()


def cookie_viewer(cookie: str) -> Viewer:
    """A credential selector, never proof of a role or an active session.

    Reuse the existing token format/hash. The SQL predicate below verifies the
    corresponding session/user inside each read's snapshot. No staff mutation
    lock is held while an untrusted public request scans a file.
    """
    if not valid_token(cookie):
        return PUBLIC_VIEWER
    return Viewer(staff_token_hash=token_hash(cookie))


def staff_access(viewer: Viewer) -> ColumnElement[bool]:
    if viewer.staff_session_id is None and viewer.staff_token_hash is None:
        return false()
    credential = (
        StaffSession.token_hash == viewer.staff_token_hash
        if viewer.staff_token_hash is not None
        else StaffSession.id == viewer.staff_session_id
    )
    return exists(
        select(StaffSession.id)
        .join(StaffUser, StaffUser.id == StaffSession.user_id)
        .where(
            credential,
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
