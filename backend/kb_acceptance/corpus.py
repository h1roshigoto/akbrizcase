"""Versioned, locked acceptance input. No runtime search/publication changes."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from app.modules.kb.import_manifest import (
    ImportSection,
    Key,
    Label,
    StrictModel,
    _object,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CorpusRejected(ValueError):
    """Closed code only: protected input must never appear in diagnostics."""


class DocumentSpec(StrictModel):
    key: Key
    version: Annotated[int, Field(ge=1)]
    title: Label
    material: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*\.txt$")]
    sha256: Digest
    visibility: Literal["public", "internal"]
    status: Literal["active", "revoked"]
    version_status: Literal["draft", "published", "retired", "revoked"]


class SectionSpec(ImportSection):
    key: Key
    document_key: Key
    version: Annotated[int, Field(ge=1)]


class QuerySpec(StrictModel):
    query_id: Key
    group: Literal["relevant", "irrelevant", "unsafe"]
    subclass: Label
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    expected_sections: list[Key]
    information_need: Label
    topic: Label
    relevance_criterion: Annotated[str, Field(min_length=1, max_length=1000)]
    expert_status: Literal["synthetic-fixture", "approved", "pending"]
    negative_behavior: Literal["no_match", "safe_quote", "handoff"] | None
    safe_sections: list[Key]


class Approval(StrictModel):
    status: Literal["synthetic-fixture", "approved", "pending", "rejected"]
    reference: Label | None
    rights_reference: Label | None
    approved_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    reviewer: Label | None


class Environment(StrictModel):
    postgresql_major: Literal[16]
    text_search_config: Literal["russian"]
    top_k: Literal[5]
    minimum_hit_rate: Literal["4/5"]
    excerpt_characters: Literal[600]


class Manifest(StrictModel):
    format: Literal["kb-acceptance"]
    format_version: Literal[1]
    corpus_id: Key
    corpus_version: Label
    status: Literal["synthetic", "expert-approved"]
    label: Label
    locked: Literal[True]
    previous_corpus_sha256: Digest | None
    revision_reason: Annotated[str, Field(min_length=1, max_length=1000)]
    queries_sha256: Digest
    sections_sha256: Digest
    documents: list[DocumentSpec]
    assistant_policy: Annotated[str, Field(min_length=1, max_length=1000)]
    expert_approval: Approval
    environment: Environment


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def parse(data: bytes) -> object:
    try:
        if not 1 <= len(data) <= 1024 * 1024:
            raise ValueError
        return json.loads(data, object_pairs_hook=_object)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CorpusRejected("INVALID_JSON") from None


@dataclass(frozen=True)
class Corpus:
    manifest: Manifest
    queries: list[QuerySpec]
    sections: list[SectionSpec]
    checksum: str

    @property
    def section_map(self) -> dict[str, SectionSpec]:
        return {s.key: s for s in self.sections}

    def admissible(self, key: str) -> bool:
        section = self.section_map[key]
        document = next(
            d
            for d in self.manifest.documents
            if (d.key, d.version) == (section.document_key, section.version)
        )
        return (
            document.visibility == "public"
            and document.status == "active"
            and document.version_status == "published"
            and section.assistant_allowed
        )


def validate(manifest_data: bytes, queries_data: bytes, sections_data: bytes) -> Corpus:
    try:
        manifest = Manifest.model_validate(parse(manifest_data))
        raw_queries, raw_sections = parse(queries_data), parse(sections_data)
        if not isinstance(raw_queries, list) or not isinstance(raw_sections, list):
            raise ValueError
        queries = [QuerySpec.model_validate(q) for q in raw_queries]
        sections = [SectionSpec.model_validate(s) for s in raw_sections]
        if (
            sha256(queries_data) != manifest.queries_sha256
            or sha256(sections_data) != manifest.sections_sha256
        ):
            raise CorpusRejected("CHECKSUM_MISMATCH")
        if (
            sum(q.group == "relevant" for q in queries) < 20
            or sum(q.group != "relevant" for q in queries) < 20
        ):
            raise CorpusRejected("INCOMPLETE_CORPUS")
        for values in (
            [q.query_id for q in queries],
            [" ".join(q.text.casefold().split()) for q in queries],
            [tuple(sorted(re.findall(r"[^\W_]+", q.text.casefold()))) for q in queries],
            [s.key for s in sections],
            [(s.document_key, s.version, s.section_key) for s in sections],
            [(d.key, d.version) for d in manifest.documents],
        ):
            if len(set(values)) != len(values):
                raise CorpusRejected("DUPLICATE_ENTRY")
        pairs = {(d.key, d.version) for d in manifest.documents}
        if {(s.document_key, s.version) for s in sections} != pairs:
            raise CorpusRejected("DOCUMENT_VERSION_MAPPING")
        for key in {d.key for d in manifest.documents}:
            editions = [d for d in manifest.documents if d.key == key]
            if (
                len({(d.title, d.visibility, d.status) for d in editions}) != 1
                or sum(d.version_status == "published" for d in editions) > 1
            ):
                raise CorpusRejected("DOCUMENT_VERSION_MAPPING")
        corpus = Corpus(manifest, queries, sections, sha256(manifest_data))
        for q in queries:
            keys = q.expected_sections + q.safe_sections
            if len(set(keys)) != len(keys) or any(
                k not in corpus.section_map or not corpus.admissible(k) for k in keys
            ):
                raise CorpusRejected("UNKNOWN_OR_FORBIDDEN_SECTION")
            if not q.text.strip() or not all(
                s.strip() for s in (q.information_need, q.topic, q.relevance_criterion)
            ):
                raise ValueError
            if q.group == "relevant":
                if not q.expected_sections or q.safe_sections or q.negative_behavior:
                    raise CorpusRejected("INVALID_JUDGEMENT")
            elif (
                q.expected_sections
                or q.negative_behavior is None
                or ((q.negative_behavior == "safe_quote") != bool(q.safe_sections))
            ):
                raise CorpusRejected("INVALID_JUDGEMENT")
        if manifest.status == "synthetic":
            a = manifest.expert_approval
            if (
                manifest.label != "SYNTHETIC ONLY"
                or a.status != "synthetic-fixture"
                or any((a.reference, a.rights_reference, a.approved_on, a.reviewer))
                or any(q.expert_status != "synthetic-fixture" for q in queries)
                or any(
                    "SYNTHETIC ONLY" not in s.title
                    or not s.text.startswith("SYNTHETIC ONLY")
                    for s in sections
                )
                or any("SYNTHETIC ONLY" not in d.title for d in manifest.documents)
            ):
                raise CorpusRejected("SYNTHETIC_LABEL_REQUIRED")
        # An expert-approved label alone deliberately grants no acceptance.
        return corpus
    except (ValidationError, ValueError, TypeError, StopIteration) as error:
        if isinstance(error, CorpusRejected):
            raise
        raise CorpusRejected("INVALID_CORPUS") from None


def load_synthetic(root: Path) -> Corpus:
    try:
        data = (root / "manifest.json").read_bytes()
        if sha256(data) != (root / "manifest.sha256").read_text().strip():
            raise CorpusRejected("MANIFEST_LOCK_MISMATCH")
        corpus = validate(
            data,
            (root / "queries.json").read_bytes(),
            (root / "sections.json").read_bytes(),
        )
        if corpus.manifest.status != "synthetic":
            raise CorpusRejected("SYNTHETIC_ONLY")
        for document in corpus.manifest.documents:
            material = (root / document.material).read_bytes()
            if sha256(material) != document.sha256:
                raise CorpusRejected("MATERIAL_CHECKSUM")
            for section in corpus.sections:
                if (section.document_key, section.version) == (
                    document.key,
                    document.version,
                ) and section.text.encode() not in material:
                    raise CorpusRejected("MATERIAL_SECTION_BINDING")
        return corpus
    except OSError:
        raise CorpusRejected("CORPUS_IO") from None
