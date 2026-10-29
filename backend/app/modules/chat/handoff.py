"""Safe support proposal; never infer a diagnostic history from stored JSON.

There is no independently verified transition ledger for a detailed summary.
Use the approved general template instead. This factory accepts no user text,
stored action, tree label or source. Call only from the public reply projector;
technical replies still go through its current snapshot/ACL guards.
"""

from app.schemas.chat import ContactSpecialistAction, SpecialistPayload

GENERAL_SUPPORT_DRAFT = (
    "Прошу специалиста связаться со мной по вопросу оборудования. "
    "Подробности опишу в обращении."
)


def support_handoff() -> ContactSpecialistAction:
    # The DTO enforces the same meaningful-text and 2000 character ceiling.
    return ContactSpecialistAction(
        type="contact_specialist",
        payload=SpecialistPayload(kind="support", comment_draft=GENERAL_SUPPORT_DRAFT),
    )
