"""Admin image changes: expensive preparation before row locks, outer UoW commit."""

from contextlib import ExitStack
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import has_role
from app.core.staff_models import StaffSession, StaffUser
from app.core.staff_sessions import StaffIdentity
from app.modules.catalog.images.pipeline import ImagePipeline
from app.modules.catalog.models import CatalogItem
from app.modules.catalog.write_service import CatalogRejected, CatalogWriteService


class CatalogImageService:
    def __init__(
        self, session: Session, actor: StaffIdentity, pipeline: ImagePipeline
    ) -> None:
        if not isinstance(actor, StaffIdentity) or not has_role(actor.user, "admin"):
            raise CatalogRejected(403, "Insufficient permissions")
        self.session = session
        self.actor = actor
        self.pipeline = pipeline

    def _authorize(self) -> None:
        # Refresh authority after all file processing. SHARE locks serialize this
        # commit with disable/logout, without the global staff advisory lock or
        # any document locks. User -> session is also disable's update order.
        user = self.session.scalar(
            select(StaffUser)
            .where(StaffUser.id == self.actor.user.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        credential = self.session.scalar(
            select(StaffSession)
            .where(
                StaffSession.id == self.actor.session.id,
                StaffSession.user_id == self.actor.user.id,
                StaffSession.token_hash == self.actor.session.token_hash,
                StaffSession.revoked_at.is_(None),
                StaffSession.expires_at > func.clock_timestamp(),
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if user is None or not has_role(user, "admin") or credential is None:
            raise CatalogRejected(403, "Insufficient permissions")

    def _item(self, identifier: UUID, expected_revision: int) -> CatalogItem:
        row = self.session.scalar(
            select(CatalogItem)
            .where(CatalogItem.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise CatalogRejected(404, "Object not found")
        if type(expected_revision) is not int or row.revision != expected_revision:
            raise CatalogRejected(409, "Catalog changed; reload its current state")
        if row.status == "archived":
            raise CatalogRejected(409, "Archived records are read-only")
        return row

    def _change(self, row: CatalogItem, key: str | None) -> None:
        if row.revision == 2147483647:
            raise CatalogRejected(409, "Revision limit reached")
        row.image_key = key
        # Reuse catalog timestamp/revision/audit contract with the actual actor.
        CatalogWriteService(self.session, self.actor)._changed(row, "edit")

    def replace(
        self,
        identifier: UUID,
        expected_revision: int,
        stream: BinaryIO,
        *,
        filename: str,
        mime: str,
        declared_size: int | None = None,
    ) -> CatalogItem:
        """No SQL/row locks until both scans, encoding and preparation finish.

        Call in a fresh shared UoW with an already authenticated identity; do not
        wrap file processing in the global staff lock. No public upload exists.
        A failed operation may leave only an unbound private orphan.
        """
        accepted = self.pipeline.accept(
            stream, filename=filename, mime=mime, declared_size=declared_size
        )
        with ExitStack() as files:
            metadata, _ = files.enter_context(self.pipeline.prepare(accepted.image_key))
            with self.session.begin_nested():
                self._authorize()
                row = self._item(identifier, expected_revision)
                current = (
                    self.pipeline.metadata(row.image_key) if row.image_key else None
                )
                if current is None or (
                    current.mime,
                    current.size_bytes,
                    current.checksum_sha256,
                ) != (metadata.mime, metadata.size_bytes, metadata.checksum_sha256):
                    self._change(row, metadata.image_key)
                # Exit integrity checks are inside the savepoint and outer UoW.
                files.close()
                return row

    def remove(self, identifier: UUID, expected_revision: int) -> CatalogItem:
        with self.session.begin_nested():
            self._authorize()
            row = self._item(identifier, expected_revision)
            if row.image_key is not None:
                self._change(row, None)
            return row
