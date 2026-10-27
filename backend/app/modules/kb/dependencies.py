"""Technical dependency preview extension point for MVP-024/028.

Only equipment manual links exist today. Future mutation providers must acquire
documents/versions -> catalog_items -> applicability, each group in stable ID
order, in the SAME outer UoW. They must never veto a security revocation.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.catalog.models import EquipmentModel


@dataclass(frozen=True)
class DependencyPreview:
    kind: str
    ids: tuple[UUID, ...]

    def technical(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "count": len(self.ids),
            "ids": [str(i) for i in self.ids],
        }


class DependencyProvider(Protocol):
    def preview(
        self, session: Session, document_id: UUID, version_ids: tuple[UUID, ...]
    ) -> DependencyPreview: ...


class EquipmentManualDependencies:
    def preview(
        self, session: Session, document_id: UUID, version_ids: tuple[UUID, ...]
    ) -> DependencyPreview:
        ids = session.scalars(
            select(EquipmentModel.id)
            .where(EquipmentModel.manual_document_id == document_id)
            .order_by(EquipmentModel.id)
        ).all()
        return DependencyPreview("equipment_models.manual_document_id", tuple(ids))
