"""Shared revision and invalidation writes; caller owns locks and transaction."""

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.staff_models import AuditEvent
from app.modules.catalog.models import CatalogItem, CatalogItemModel


def bump_revision(session: Session, item: CatalogItem) -> None:
    # Integer overflow must fail/rollback rather than leave a stale revision.
    item.revision += 1
    now = session.scalar(select(func.clock_timestamp()))
    assert now is not None
    item.updated_at = now


def clear_permission(link: CatalogItemModel) -> None:
    link.public_allowed = False
    link.published_by = None
    link.published_at = None


def invalidate_links(
    session: Session,
    candidates: Select[tuple[CatalogItemModel]],
    actor_id: UUID,
    reason: str,
) -> None:
    """Parents already locked; then ALL items, then ALL links in UUID order.

    Clear approved_at too: restoring an edition can never reuse its old review.
    The old expert/reference/time remain in append-only audit, not fake fields.
    """
    pairs = list(session.scalars(candidates))
    if not pairs:
        return
    items = list(
        session.scalars(
            select(CatalogItem)
            .where(CatalogItem.id.in_({p.item_id for p in pairs}))
            .order_by(CatalogItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    # Requery after waiting: another request may have revoked or replaced a link.
    links = list(
        session.scalars(
            candidates.order_by(CatalogItemModel.item_id, CatalogItemModel.model_id)
            .with_for_update(of=CatalogItemModel)
            .execution_options(populate_existing=True)
        )
    )
    affected: set[UUID] = set()
    events: list[AuditEvent] = []
    for link in links:
        if not (link.public_allowed or link.approved_at is not None):
            continue
        was_public = link.public_allowed
        details: dict[str, str | bool | int] = {
            "model_id": str(link.model_id),
            "evidence_section_id": str(link.evidence_section_id),
            "before_public_allowed": was_public,
            "after_public_allowed": False,
            "reason": reason,
            "expert_name": link.expert_name or "",
            "review_reference": link.review_reference or "",
            "approved_at": link.approved_at.isoformat() if link.approved_at else "",
            "before_published_by": str(link.published_by) if link.published_by else "",
            "before_published_at": link.published_at.isoformat()
            if link.published_at
            else "",
            "after_published_by": "",
            "after_published_at": "",
        }
        clear_permission(link)
        link.approved_at = None
        affected.add(link.item_id)
        events.append(
            AuditEvent(
                actor_id=actor_id,
                action="applicability.invalidate",
                entity_type="catalog_item",
                entity_id=link.item_id,
                result="success",
                details=details,
            )
        )
    revisions: dict[UUID, int] = {}
    for item in items:
        if item.id in affected:
            bump_revision(session, item)
            revisions[item.id] = item.revision
    for event in events:
        assert event.entity_id is not None
        event.details["revision"] = revisions[event.entity_id]
    session.add_all(events)
    session.flush()
