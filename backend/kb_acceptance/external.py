"""Read-only protected corpus adapter; never imports, publishes or grants admission.

An external decision must already exist and be checked in its protected registry.
Software validates bindings, not expertise or legal rights.
"""

import argparse
import json
import os
import stat
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import Database, DatabaseSettings
from app.modules.chat.diagnosis import DiagnosisState
from app.modules.chat.mode_router import ModeRouter
from app.modules.kb.access import PUBLIC_VIEWER
from app.modules.kb.files import FilePipeline
from app.modules.kb.import_manifest import Key, Label, StrictModel, checked_record
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import KnowledgeReadService, source_ref
from app.schemas.sources import KnowledgeHit, KnowledgeQuery
from kb_acceptance.corpus import (
    Corpus,
    CorpusRejected,
    Digest,
    Manifest,
    QuerySpec,
    SectionSpec,
    canonical,
    sha256,
    validate,
)
from kb_acceptance.measure import Binding, Observation, measure


class Link(StrictModel):
    document_code: Key
    document_id: str
    document_version_id: str
    section_id: str


class ExpertDecision(StrictModel):
    decision: str
    corpus_sha256: Digest
    mapping_sha256: Digest
    relevant_query_ids: list[Key]
    negative_query_ids: list[Key]
    expected_sections: dict[str, list[Key]]
    reference: Label
    rights_reference: Label
    reviewer: Label
    approved_on: str
    quote_boundaries_checked: bool
    no_unverified_instructions: bool
    full_file_public_allowed: bool


class ExternalPacket(StrictModel):
    manifest: Manifest
    queries: list[QuerySpec]
    sections: list[SectionSpec]
    mapping: dict[str, Link]
    decision: ExpertDecision | None = None


def verify_decision(packet: ExternalPacket) -> Corpus:
    corpus = validate(
        canonical(packet.manifest.model_dump()),
        canonical([q.model_dump() for q in packet.queries]),
        canonical([s.model_dump() for s in packet.sections]),
    )
    a, d = packet.manifest.expert_approval, packet.decision
    if (
        corpus.manifest.status != "expert-approved"
        or a.status != "approved"
        or d is None
    ):
        raise CorpusRejected("EXTERNAL_APPROVAL_REQUIRED")
    if (
        d.decision != "APPROVED"
        or d.corpus_sha256 != corpus.checksum
        or d.mapping_sha256
        != sha256(canonical({k: v.model_dump() for k, v in packet.mapping.items()}))
        or set(packet.mapping) != set(corpus.section_map)
        or (d.reference, d.rights_reference, d.reviewer, d.approved_on)
        != (a.reference, a.rights_reference, a.reviewer, a.approved_on)
        or not all(
            (
                d.quote_boundaries_checked,
                d.no_unverified_instructions,
                d.full_file_public_allowed,
            )
        )
        or any(q.expert_status != "approved" for q in corpus.queries)
    ):
        raise CorpusRejected("EXTERNAL_APPROVAL_BINDING")
    for actual, expected in (
        (
            d.relevant_query_ids,
            [q.query_id for q in corpus.queries if q.group == "relevant"],
        ),
        (
            d.negative_query_ids,
            [q.query_id for q in corpus.queries if q.group != "relevant"],
        ),
    ):
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            raise CorpusRejected("EXTERNAL_QUERY_APPROVAL")
    if d.expected_sections != {
        q.query_id: q.expected_sections for q in corpus.queries if q.group == "relevant"
    }:
        raise CorpusRejected("EXTERNAL_SECTION_APPROVAL")
    try:
        if date.fromisoformat(d.approved_on) > datetime.now(UTC).date():
            raise ValueError
    except ValueError:
        raise CorpusRejected("EXTERNAL_APPROVAL_DATE") from None
    if "synthetic" in canonical(packet.model_dump()).decode().casefold():
        raise CorpusRejected("SYNTHETIC_IS_NOT_REAL_EVIDENCE")
    return corpus


def existing_mapping(
    session: Session, corpus: Corpus, links: dict[str, Link], pipeline: FilePipeline
) -> dict[str, Binding]:
    result = {}
    checked: set[UUID] = set()
    for key, link in links.items():
        spec = corpus.section_map[key]
        doc_spec = next(
            d
            for d in corpus.manifest.documents
            if (d.key, d.version) == (spec.document_key, spec.version)
        )
        try:
            document = session.get(Document, UUID(link.document_id))
            version = session.get(DocumentVersion, UUID(link.document_version_id))
            section = session.get(DocumentSection, UUID(link.section_id))
        except ValueError:
            raise CorpusRejected("WRONG_DATABASE_MAPPING") from None
        if (
            document is None
            or version is None
            or section is None
            or (
                document.code,
                document.title,
                document.visibility,
                document.status,
                version.document_id,
                version.version,
                version.status,
                version.checksum_sha256,
                section.document_version_id,
                section.section_key,
                section.title,
                section.text,
                section.page_from,
                section.page_to,
                section.assistant_allowed,
            )
            != (
                link.document_code,
                doc_spec.title,
                doc_spec.visibility,
                doc_spec.status,
                document.id,
                spec.version,
                doc_spec.version_status,
                doc_spec.sha256,
                version.id,
                spec.section_key,
                spec.title,
                spec.text,
                spec.page_from,
                spec.page_to,
                spec.assistant_allowed,
            )
        ):
            raise CorpusRejected("WRONG_DATABASE_MAPPING")
        if version.id not in checked:
            with pipeline.prepare(version.file_key) as (metadata, _descriptor):
                if (metadata.checksum_sha256, metadata.size_bytes, metadata.mime) != (
                    doc_spec.sha256,
                    version.size_bytes,
                    version.mime,
                ):
                    raise CorpusRejected("MATERIAL_CHECKSUM")
            checked.add(version.id)
        result[key] = Binding(
            spec.document_key,
            spec.version,
            KnowledgeHit(
                excerpt=section.text[:600],
                source=source_ref(document, version, section),
            ),
        )
    return result


def run_external(packet_path: Path, report_path: Path) -> dict[str, object]:
    parent = report_path.parent.resolve(strict=True)
    info = parent.stat()
    checkout = Path(__file__).resolve().parents[2]
    if (
        parent.is_relative_to(checkout)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise CorpusRejected("PRIVATE_REPORT_DIRECTORY_REQUIRED")
    with checked_record(packet_path, ExternalPacket) as packet:
        corpus = verify_decision(packet)
        database = Database(DatabaseSettings.from_environment())
        try:
            with database.unit_of_work() as session:
                session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                version = str(session.scalar(text("SHOW server_version")))
                if not version.startswith("16."):
                    raise CorpusRejected("POSTGRESQL_16_REQUIRED")
                mapping = existing_mapping(
                    session, corpus, packet.mapping, FilePipeline.from_environment()
                )
                reader, router = (
                    KnowledgeReadService(session, PUBLIC_VIEWER),
                    ModeRouter(session),
                )
                observations = [
                    Observation(
                        q.query_id,
                        reader.search(KnowledgeQuery(q=q.text)),
                        router.route(
                            DiagnosisState(mode="knowledge"),
                            {"type": "text", "value": q.text},
                        ).reply,
                    )
                    for q in corpus.queries
                ]
                report = measure(corpus, mapping, observations, session=session)
                report.update(
                    {
                        "postgresql_version": version,
                        "measured_schema_revisions": [
                            session.scalar(
                                text("SELECT version_num FROM app.alembic_version")
                            )
                        ],
                        "REAL_EXPERT_CORPUS": "APPROVED",
                        "REAL_TOP5_ACCEPTANCE": report["technical_result"],
                        "REAL_HIT_RATE_AT_5": report["hit_rate_at_5"],
                        "REAL_NEGATIVE_SAFETY": "PASS"
                        if report["negative_passed"] == report["negative_count"]
                        and not report["acl_incidents"]
                        and report["complete"]
                        else "FAIL",
                        "EXPERT_REVIEW": "APPROVED",
                        "SYNTHETIC_CORPUS_VALIDATION": "NOT_APPLICABLE",
                        "SYNTHETIC_NEGATIVE_SAFETY": "NOT_APPLICABLE",
                        "mapping": {
                            k: v.hit.source.model_dump(mode="json")
                            for k, v in mapping.items()
                        },
                        "http_contract": (
                            "NOT_MEASURED; synthetic HTTP suite is separate"
                        ),
                    }
                )
        finally:
            database.close()
    # Final protected input check and UoW complete before any success artifact.
    fd = os.open(
        parent / report_path.name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    blocked = {
        "REAL_EXPERT_CORPUS": "BLOCKED",
        "REAL_TOP5_ACCEPTANCE": "BLOCKED",
        "REAL_HIT_RATE_AT_5": "NOT_MEASURED",
        "REAL_NEGATIVE_SAFETY": "BLOCKED",
        "EXPERT_REVIEW": "PENDING",
        "PRODUCTION_ADMISSION": "BLOCKED",
    }
    if args.packet is None or args.report is None:
        print(json.dumps(blocked))
        return 2
    try:
        report = run_external(args.packet, args.report)
        print(json.dumps({key: report[key] for key in blocked}))
        return 0 if report["technical_result"] == "PASS" else 1
    except Exception:
        print(json.dumps({**blocked, "error": "EXTERNAL_ACCEPTANCE_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
