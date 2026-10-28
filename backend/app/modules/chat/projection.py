"""Fail-closed public read boundary, to be extended by MVP-034.

No stored input/response, tree identifiers, source references, buttons or catalog
snapshots are trusted here. Enabling them requires source/action revalidation in
the shared projector for new replies, history and replay. Staff cookies never
change this public projection.
"""

from app.schemas.chat import (
    ChatMode,
    ChatReply,
    ContactSpecialistAction,
    SpecialistPayload,
)


def safe_reply(mode: ChatMode) -> ChatReply:
    return ChatReply(
        mode=mode,
        text="Чат пока недоступен. Обратитесь к специалисту.",
        buttons=[],
        sources=[],
        items=[],
        actions=[
            ContactSpecialistAction(
                type="contact_specialist", payload=SpecialistPayload(kind="support")
            )
        ],
        resolution="handoff",
    )
