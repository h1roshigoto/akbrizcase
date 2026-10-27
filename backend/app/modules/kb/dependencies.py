"""Technical dependency preview extension point for MVP-024/028.

Providers acquire
documents/versions -> catalog_items -> applicability, each group in stable ID
order, in the SAME outer UoW. They must never veto a security revocation.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.catalog.applicability_mutations import invalidate_links
from app.modules.catalog.models import CatalogItemModel, EquipmentModel
from app.modules.kb.models import DocumentSection


@dataclass(frozen=True)
class DependencyPreview:
    kind: str
    ids: tuple[UUID, ...]
    links: tuple[tuple[UUID, UUID, UUID], ...] = ()

    def technical(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "count": len(self.ids),
            "ids": [str(i) for i in self.ids],
        }
        if self.kind == "catalog_item_models.evidence_section_id":
            result["links"] = [
                {"item_id": str(i), "model_id": str(m), "section_id": str(s)}
                for i, m, s in self.links
            ]
            result["link_count"] = len(self.links)
        return result


class DependencyProvider(Protocol):
    def preview(
        self, session: Session, document_id: UUID, version_ids: tuple[UUID, ...]
    ) -> DependencyPreview: ...

    def invalidate(
        self,
        session: Session,
        version_ids: tuple[UUID, ...],
        actor_id: UUID,
        reason: str,
    ) -> None: ...


class EquipmentManualDependencies:
    def invalidate(
        self,
        session: Session,
        version_ids: tuple[UUID, ...],
        actor_id: UUID,
        reason: str,
    ) -> None:
        # Manual FK is historical reference, not a public applicability claim.
        pass

    def preview(
        self, session: Session, document_id: UUID, version_ids: tuple[UUID, ...]
    ) -> DependencyPreview:
        ids = session.scalars(
            select(EquipmentModel.id)
            .where(EquipmentModel.manual_document_id == document_id)
            .order_by(EquipmentModel.id)
        ).all()
        return DependencyPreview("equipment_models.manual_document_id", tuple(ids))


class ApplicabilityDependencies:
    def preview(
        self, session: Session, document_id: UUID, version_ids: tuple[UUID, ...]
    ) -> DependencyPreview:
        links = session.execute(
            select(
                CatalogItemModel.item_id,
                CatalogItemModel.model_id,
                CatalogItemModel.evidence_section_id,
            )
            .join(
                DocumentSection,
                DocumentSection.id == CatalogItemModel.evidence_section_id,
            )
            .where(DocumentSection.document_version_id.in_(version_ids))
            .order_by(CatalogItemModel.item_id, CatalogItemModel.model_id)
        ).all()
        return DependencyPreview(
            "catalog_item_models.evidence_section_id",
            tuple(sorted({r[0] for r in links})),
            tuple((r[0], r[1], r[2]) for r in links),
        )

    def invalidate(
        self,
        session: Session,
        version_ids: tuple[UUID, ...],
        actor_id: UUID,
        reason: str,
    ) -> None:
        invalidate_links(
            session,
            select(CatalogItemModel).where(
                CatalogItemModel.evidence_section_id.in_(
                    select(DocumentSection.id).where(
                        DocumentSection.document_version_id.in_(version_ids)
                    )
                )
            ),
            actor_id,
            reason,
        )
