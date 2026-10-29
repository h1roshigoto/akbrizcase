"""Consent version and explicit acceptance; no stored consent is client-selected."""

from pydantic import Field

from app.schemas.common import DTO, Checksum, Confirmed, Text200


class ConsentText(DTO):
    version: Text200
    purpose: Text200
    text: str = Field(min_length=1, max_length=100000)
    checksum: Checksum


class ConsentAcceptance(DTO):
    version: Text200
    accepted: Confirmed


class PrivacyPolicy(DTO):
    version: Text200
    text: str = Field(min_length=1, max_length=100000)
    checksum: Checksum
    withdrawal_instructions: str = Field(min_length=1, max_length=4000)
    consent_version: Text200
    synthetic: bool
