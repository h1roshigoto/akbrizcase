"""Approved release configuration; no implicit production retention or consent."""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import Text200
from app.schemas.consent import ConsentText

Days = Annotated[int, Field(strict=True, ge=1, le=36500)]
Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,199}$")]


class RetentionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chat_access_hours: Literal[24]
    chat_storage_days: Days
    order_pii_after_terminal_days: Days
    linked_snapshots_after_terminal_days: Days
    commercial_snapshots_after_terminal_days: Days
    consent_evidence: Literal["until_order_deleted"]
    consent_text: Literal["immutable_permanent"]
    audit_days: Days
    technical_logs_days: Days
    outbox_sent_days: Days
    outbox_failed_days: Days
    outbox_pending: Literal["manual_review_no_ttl"]
    outbox_pending_review_hours: Annotated[int, Field(strict=True, ge=1, le=24)]
    backup_days: Days
    deletion_journal_days: Days
    expert_review_after_content_retired_days: Days
    publication_evidence_after_content_retired_days: Days
    open_order_review_days: Days
    open_orders: Literal["manual_review_no_ttl"]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            self.linked_snapshots_after_terminal_days
            > self.order_pii_after_terminal_days
        ):
            raise ValueError(
                "Linkable snapshots cannot outlive the PII erasure deadline"
            )
        if self.deletion_journal_days < max(14, self.backup_days):
            raise ValueError("Deletion journal must cover backup horizon and 14 days")
        return self


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Text200
    text: Annotated[str, Field(min_length=1, max_length=100000)]
    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    withdrawal_instructions: Annotated[str, Field(min_length=1, max_length=4000)]

    @model_validator(mode="after")
    def exact_text(self) -> Self:
        if not self.text.strip() or not self.withdrawal_instructions.strip():
            raise ValueError("Missing document content")
        if digest(self.text) != self.checksum:
            raise ValueError("Policy checksum mismatch")
        return self


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PrivacyBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_status: Literal["APPROVED", "SYNTHETIC ONLY"]
    approval_ref: Reference
    approved_at: AwareDatetime
    retention: RetentionSettings
    policy: PolicyDocument
    consent: ConsentText

    @model_validator(mode="after")
    def approved_content(self) -> Self:
        if digest(self.consent.text) != self.consent.checksum:
            raise ValueError("Consent checksum mismatch")
        if self.approved_at.utcoffset() is None or not self.consent.text.strip():
            raise ValueError("Invalid approval")
        if self.approval_status == "APPROVED" and any(
            "synthetic" in value.lower()
            for value in (
                self.consent.version,
                self.consent.purpose,
                self.consent.text,
                self.policy.version,
                self.policy.text,
                self.policy.withdrawal_instructions,
            )
        ):
            raise ValueError("Synthetic documents cannot authorize production")
        return self

    def checksum(self) -> str:
        return digest(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def valid_at(self, now: datetime) -> bool:
        return self.approved_at <= now


def load_bundle(path: Path) -> PrivacyBundle:
    # Bound input and reject duplicate keys instead of silently overriding approval.
    raw = path.read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError("Privacy configuration too large")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate configuration field")
            result[key] = value
        return result

    return PrivacyBundle.model_validate(json.loads(raw, object_pairs_hook=unique))


def production_bundle() -> PrivacyBundle:
    try:
        bundle = load_bundle(Path(os.environ["PRIVACY_CONFIG_FILE"]))
        if bundle.approval_status != "APPROVED":
            raise ValueError
        return bundle
    except (KeyError, OSError, ValueError, UnicodeError):
        # Do not echo private file contents, evidence references or validation input.
        raise ValueError("Approved privacy configuration unavailable") from None
