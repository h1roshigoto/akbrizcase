"""Validate proposed actions against current public reads, without performing them.

This is a service boundary, not an HTTP executor or a history/replay projector.
The caller owns the transaction. No staff viewer or arbitrary URL is accepted.
"""

from collections.abc import Mapping
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import CatalogNotFound, CatalogService
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.read_service import KnowledgeReadService, SourceNotFound
from app.schemas.chat import (
    ChatAction,
    CommercialFormPayload,
    ContactSpecialistAction,
    OpenDocumentAction,
    OpenItemAction,
    OpenRequestFormAction,
)

_ACTION = TypeAdapter[ChatAction](ChatAction)


class ActionRejected(ValueError):
    """One non-disclosing error for malformed, missing and nonpublic targets."""


class ActionValidator:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.catalog = CatalogService(CatalogRepository(session))
        self.knowledge = KnowledgeReadService(session, PUBLIC_VIEWER)

    def validate(
        self,
        proposed: ChatAction | Mapping[str, object],
        *,
        model_id: UUID | None = None,
    ) -> ChatAction:
        # Reparse DTOs as well: callers may mutate them or use model_construct.
        raw = (
            dict(proposed)
            if isinstance(proposed, Mapping)
            else proposed.model_dump(warnings=False)
        )
        try:
            action = _ACTION.validate_python(raw)
            with self.session.no_autoflush:
                if isinstance(action, OpenItemAction):
                    self.catalog.item(action.payload.item_id, model_id)
                elif isinstance(action, OpenDocumentAction):
                    current = self.knowledge.get_source(action.payload.section_id)
                    if current != action.payload:
                        raise ActionRejected("ACTION_UNAVAILABLE")
                elif isinstance(action, OpenRequestFormAction):
                    if isinstance(action.payload, CommercialFormPayload):
                        for item in action.payload.items:
                            self.catalog.item(item.item_id, model_id)
                    elif action.payload.comment_draft is not None:
                        raise ActionRejected("ACTION_UNAVAILABLE")
                elif isinstance(action, ContactSpecialistAction):
                    # Only the projector's handoff factory generates drafts.
                    # Caller/stored text never becomes an approved proposal.
                    if action.payload.comment_draft is not None:
                        raise ActionRejected("ACTION_UNAVAILABLE")
            return action
        except (ValidationError, CatalogNotFound, SourceNotFound):
            raise ActionRejected("ACTION_UNAVAILABLE") from None
