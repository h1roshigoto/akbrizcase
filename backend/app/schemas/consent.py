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
