"""Bounded JSON operator manifest, outside Git. Never include input in errors."""

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.kb.files.process import FileRejected

MAX_MANIFEST = 1024 * 1024
Label = Annotated[str, Field(min_length=1, max_length=200)]
Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ImportSection(StrictModel):
    section_key: Key
    title: Label
    page_from: Annotated[int, Field(ge=1, le=100)] | None = None
    page_to: Annotated[int, Field(ge=1, le=100)] | None = None
    text: Annotated[str, Field(min_length=1, max_length=32000)]
    assistant_allowed: bool = False

    @model_validator(mode="after")
    def valid_content(self) -> "ImportSection":
        if not self.title.strip() or not self.text.strip():
            raise ValueError("EMPTY_FIELD")
        if (self.page_from is None) != (self.page_to is None) or (
            self.page_from is not None
            and self.page_to is not None
            and self.page_to < self.page_from
        ):
            raise ValueError("PAGES_INVALID")
        # A conservative, checkable deny rule, never expert approval. No rule
        # automatically enables a section; every true flag is manual input.
        if self.assistant_allowed and re.search(
            r"под\s+напряж|обход\w*\s+защит|ремонт|электр|"
            r"live\s+(?:wire|repair)|bypass|energiz|electrical",
            self.title + " " + self.text,
            re.I,
        ):
            raise ValueError("ASSISTANT_UNSAFE")
        return self


class ImportManifest(StrictModel):
    code: Key
    title: Label
    kind: Literal["manual", "regulation", "article", "other"]
    visibility: Literal["public", "internal"]
    version: Annotated[int, Field(ge=1, le=2147483647)]
    checksum_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    preparation: Literal["manual", "pdf-text"] = "manual"
    sections: Annotated[list[ImportSection], Field(max_length=100)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def valid_sections(self) -> "ImportManifest":
        if not self.title.strip():
            raise ValueError("EMPTY_FIELD")
        keys = [section.section_key for section in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("SECTION_DUPLICATE")
        if self.preparation == "pdf-text" and self.sections:
            raise ValueError("PREPARATION_CONFLICT")
        if self.preparation == "manual" and not self.sections:
            raise ValueError("SECTIONS_REQUIRED")
        return self


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def parse_manifest(data: bytes) -> ImportManifest:
    if not 1 <= len(data) <= MAX_MANIFEST:
        raise FileRejected("MANIFEST_INVALID")
    try:
        value = json.loads(data, object_pairs_hook=_object)
        return ImportManifest.model_validate(value)
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise FileRejected("MANIFEST_INVALID") from None


def _open(path: Path, *, private: bool) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise FileRejected("OPERATOR_FILE_INVALID") from None
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (private and (info.st_uid != os.geteuid() or info.st_mode & 0o077))
    ):
        os.close(descriptor)
        raise FileRejected("OPERATOR_FILE_INVALID")
    return descriptor


@contextmanager
def operator_file(path: Path) -> Iterator[int]:
    descriptor = _open(path, private=False)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def checked_record[Record: StrictModel](
    path: Path, model: type[Record]
) -> Iterator[Record]:
    """Private bounded JSON input, including exit checks before the caller commits."""
    descriptor = _open(path, private=True)
    try:
        data = os.read(descriptor, MAX_MANIFEST + 1)
        try:
            if not 1 <= len(data) <= MAX_MANIFEST:
                raise ValueError
            record = model.model_validate(json.loads(data, object_pairs_hook=_object))
        except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
            raise FileRejected("MANIFEST_INVALID") from None
        yield record
        # Both in-place edits and replacement/rename must fail before DB commit.
        other = _open(path, private=True)
        try:
            first, second = os.fstat(descriptor), os.fstat(other)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if (
                (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino)
                or hashlib.sha256(os.read(descriptor, MAX_MANIFEST + 1)).digest()
                != hashlib.sha256(data).digest()
                or os.read(other, MAX_MANIFEST + 1) != data
            ):
                raise FileRejected("MANIFEST_CHANGED")
        finally:
            os.close(other)
    finally:
        os.close(descriptor)


@contextmanager
def checked_manifest(path: Path) -> Iterator[ImportManifest]:
    with checked_record(path, ImportManifest) as manifest:
        yield manifest
