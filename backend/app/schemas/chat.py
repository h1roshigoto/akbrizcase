"""Discriminated chat inputs/actions. They never submit orders automatically."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.catalog import CatalogCard
from app.schemas.common import (
    DTO,
    Code,
    Comment,
    PositiveInt,
    Quantity,
    SecretToken,
    Text200,
    UTCDateTime,
    UUIDValue,
    Version,
    nonblank,
)
from app.schemas.sources import SourceRef

ChatMode = Literal["diagnose", "knowledge", "commerce"]
Resolution = Literal["question", "results", "no_match", "handoff", "completed"]


class ChatScenario(DTO):
    key: Code
    title: Text200
    model_id: UUIDValue


class ChatScenarios(DTO):
    items: Annotated[list[ChatScenario], Field(max_length=10)]


class TextInput(DTO):
    type: Literal["text"]
    value: Annotated[str, StringConstraints(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def meaningful_text(self) -> "TextInput":
        nonblank(self.value)
        return self


class SelectModeInput(DTO):
    type: Literal["select_mode"]
    mode: ChatMode


class StartDiagnosisInput(DTO):
    type: Literal["start_diagnosis"]
    model_id: UUIDValue
    tree_key: Code


class ButtonInput(DTO):
    type: Literal["button"]
    option_id: Code


class FeedbackInput(DTO):
    type: Literal["feedback"]
    useful: bool


ChatInput = Annotated[
    TextInput | SelectModeInput | StartDiagnosisInput | ButtonInput | FeedbackInput,
    Field(discriminator="type"),
]


class ChatButton(DTO):
    id: Code
    label: Text200
    input: ChatInput


class SuggestedItem(DTO):
    item_id: UUIDValue
    qty: Quantity


class OpenItemPayload(DTO):
    item_id: UUIDValue


class OpenItemAction(DTO):
    type: Literal["open_item"]
    payload: OpenItemPayload


class OpenDocumentAction(DTO):
    type: Literal["open_document"]
    payload: SourceRef


class CommercialFormPayload(DTO):
    kind: Literal["commercial"]
    items: Annotated[list[SuggestedItem], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def unique_items(self) -> "CommercialFormPayload":
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("suggested item_id values must be unique")
        return self


class SupportFormPayload(DTO):
    kind: Literal["support"]
    items: Annotated[list[SuggestedItem], Field(max_length=0)]
    comment_draft: Comment | None = None


class OpenRequestFormAction(DTO):
    type: Literal["open_request_form"]
    payload: Annotated[
        CommercialFormPayload | SupportFormPayload, Field(discriminator="kind")
    ]


class SpecialistPayload(DTO):
    kind: Literal["support"]
    comment_draft: Comment | None = None


class ContactSpecialistAction(DTO):
    type: Literal["contact_specialist"]
    payload: SpecialistPayload


ChatAction = Annotated[
    OpenItemAction
    | OpenDocumentAction
    | OpenRequestFormAction
    | ContactSpecialistAction,
    Field(discriminator="type"),
]


class ChatReply(DTO):
    mode: ChatMode
    text: str = Field(min_length=1, max_length=10000)
    buttons: list[ChatButton]
    sources: list[SourceRef]
    items: list[CatalogCard]
    actions: list[ChatAction]
    resolution: Resolution


class ChatSessionCreate(DTO):
    mode: ChatMode


class ChatSessionCreated(DTO):
    session_id: UUIDValue
    session_token: SecretToken = Field(repr=False)
    state_version: Literal[0]
    expires_at: UTCDateTime
    reply: ChatReply

    @model_validator(mode="before")
    @classmethod
    def strict_initial_version(cls, value: object) -> object:
        if isinstance(value, dict) and type(value.get("state_version")) is not int:
            raise ValueError("initial state_version must be the integer zero")
        return value


class ChatMessageCreate(DTO):
    session_id: UUIDValue
    request_id: UUIDValue
    expected_version: Version
    input: ChatInput


class ChatMessageResult(DTO):
    message_id: UUIDValue
    state_version: PositiveInt
    reply: ChatReply


class ChatExchangeRead(DTO):
    """Public exchange metadata and a projected reply, never the stored JSON."""

    message_id: UUIDValue
    state_version: PositiveInt
    created_at: UTCDateTime
    reply: ChatReply


class ChatSessionRead(DTO):
    session_id: UUIDValue
    mode: ChatMode
    state_version: Version
    outcome: Literal["resolved", "handoff", "unresolved"] | None
    created_at: UTCDateTime
    last_activity_at: UTCDateTime
    expires_at: UTCDateTime
    reply: ChatReply
    messages: Annotated[list[ChatExchangeRead], Field(max_length=50)]
    next_before: UUIDValue | None
