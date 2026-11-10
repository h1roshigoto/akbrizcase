"""Section-level hit@5 with a fixed denominator and independent safety gates."""

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.chat.diagnosis import HANDOFF_TEXT
from app.modules.chat.handoff import support_handoff
from app.modules.chat.mode_router import mode_buttons, specialist
from app.modules.kb.models import Document, DocumentSection, DocumentVersion
from app.modules.kb.read_service import source_ref
from app.schemas.chat import ChatReply
from app.schemas.sources import KnowledgeHit, KnowledgePage, SourceRef
from kb_acceptance.corpus import Corpus, CorpusRejected

QUOTE_HEADING = "Цитаты из опубликованных материалов (не диагноз):\n\n"
NO_MATCH = (
    "В опубликованных материалах подтверждения не найдено. "
    "Обратитесь к специалисту или выберите другой режим."
)
PROJECTED_NO_MATCH = (
    "В опубликованных материалах подтверждения не найдено. Обратитесь к специалисту."
)


@dataclass(frozen=True)
class Binding:
    document_key: str
    version_number: int
    hit: KnowledgeHit


@dataclass(frozen=True)
class Observation:
    query_id: str
    page: KnowledgePage | None = None
    reply: ChatReply | None = None
    error: bool = False
    http_matches: bool = True
    error_kind: Literal["db", "http"] | None = None


def check_mapping(corpus: Corpus, mapping: dict[str, Binding]) -> None:
    if set(mapping) != set(corpus.section_map) or len(
        {b.hit.source.section_id for b in mapping.values()}
    ) != len(mapping):
        raise CorpusRejected("INCOMPLETE_MAPPING")
    documents: dict[str, UUID] = {}
    versions: dict[tuple[str, int], UUID] = {}
    for key, binding in mapping.items():
        section = corpus.section_map[key]
        source = binding.hit.source
        if (binding.document_key, binding.version_number) != (
            section.document_key,
            section.version,
        ) or (
            source.section,
            source.page_from,
            source.page_to,
            binding.hit.excerpt,
            source.url,
        ) != (
            section.title,
            section.page_from,
            section.page_to,
            section.text[:600],
            f"/api/v1/documents/{source.document_id}/versions/"
            f"{source.document_version_id}/file",
        ):
            raise CorpusRejected("WRONG_MAPPING")
        if (
            documents.setdefault(section.document_key, source.document_id)
            != (source.document_id)
            or versions.setdefault(
                (section.document_key, section.version), source.document_version_id
            )
            != source.document_version_id
        ):
            raise CorpusRejected("WRONG_MAPPING")
    if len(set(documents.values())) != len(documents) or len(
        set(versions.values())
    ) != len(versions):
        raise CorpusRejected("WRONG_MAPPING")


def quoted_text(page: KnowledgePage) -> str:
    return QUOTE_HEADING + "\n\n".join(
        f"[{index}] {hit.excerpt}" for index, hit in enumerate(page.items, 1)
    )


def reply_matches(
    page: KnowledgePage, reply: ChatReply, *, allow_handoff: bool = False
) -> bool:
    if reply.mode != "knowledge" or reply.items:
        return False
    if allow_handoff and reply.resolution == "handoff":
        return (
            reply.text == HANDOFF_TEXT
            and not reply.sources
            and reply.actions in ([specialist()], [support_handoff()])
            and reply.buttons in ([], mode_buttons())
        )
    if reply.buttons != mode_buttons():
        return False
    if not page.items:
        return (
            reply.resolution == "no_match"
            and reply.text in (NO_MATCH, PROJECTED_NO_MATCH)
            and not reply.sources
            and reply.actions in ([specialist()], [support_handoff()])
        )
    return (
        reply.resolution == "results"
        and reply.text == quoted_text(page)
        and reply.sources == [h.source for h in page.items]
        and len(reply.actions) == len(page.items)
        and all(
            action.type == "open_document" and action.payload == hit.source
            for action, hit in zip(reply.actions, page.items, strict=True)
        )
    )


def verify_public_source(
    session: Session, source: SourceRef, excerpt: str | None = None
) -> Literal["allowed", "forbidden", "mismatch", "unverified"]:
    """Independent database evidence, never a caller-provided UUID allowlist.

    The external runner supplies its read-only REPEATABLE READ session. Join
    through the actual section binding before comparing the claimed SourceRef.
    Missing evidence invalidates the run but does not prove an ACL incident.
    """
    row = session.execute(
        select(Document, DocumentVersion, DocumentSection)
        .select_from(DocumentSection)
        .join(
            DocumentVersion, DocumentVersion.id == DocumentSection.document_version_id
        )
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentSection.id == source.section_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        return "unverified"
    document, version, section = row
    if (
        document.visibility != "public"
        or document.status != "active"
        or version.status != "published"
        or section.assistant_allowed is not True
    ):
        return "forbidden"
    if source != source_ref(document, version, section) or (
        excerpt is not None and excerpt != section.text[:600]
    ):
        return "mismatch"
    return "allowed"


def measure(
    corpus: Corpus,
    mapping: dict[str, Binding],
    observations: list[Observation],
    *,
    session: Session | None = None,
) -> dict[str, object]:
    check_mapping(corpus, mapping)
    ids = {b.hit.source.section_id: key for key, b in mapping.items()}

    def check_source(source: SourceRef, excerpt: str | None = None) -> str:
        key = ids.get(source.section_id)
        if session is not None:
            state = verify_public_source(session, source, excerpt)
            if state != "allowed":
                return state
        if key is None:
            return "allowed" if session is not None else "unverified"
        if not corpus.admissible(key):
            return "forbidden"
        expected = mapping[key].hit
        if source != expected.source or (
            excerpt is not None and excerpt != expected.excerpt
        ):
            return "mismatch"
        return "allowed"

    by_id: dict[str, Observation] = {}
    faults: list[str] = []
    for observation in observations:
        if observation.query_id in by_id:
            faults.append("DUPLICATE_RESULT")
        by_id[observation.query_id] = observation
    if set(by_id) - {q.query_id for q in corpus.queries}:
        faults.append("UNKNOWN_RESULT")
    rows: list[dict[str, object]] = []
    hits, negatives, acl_incidents = 0, 0, 0
    page_acl_incidents, reply_acl_incidents = 0, 0
    reciprocal = Fraction(0)
    for q in corpus.queries:
        found = by_id.get(q.query_id)
        valid = bool(
            found
            and not found.error
            and found.error_kind is None
            and found.http_matches
            and found.page is not None
            and found.reply is not None
        )
        rank = None
        # None preserves an unmapped competitor's true position without
        # exporting its UUID or treating it as an approved negative quote.
        returned: list[str | None] = []
        page_acl, reply_acl = 0, 0
        reply_valid = False
        if found and found.page is not None:
            page = found.page
            valid &= (
                page.limit == 5
                and page.offset == 0
                and len(page.items) <= 5
                and page.total >= len(page.items)
                and len({h.source.section_id for h in page.items}) == len(page.items)
                and len(page.items) == min(5, page.total)
            )
            for hit in page.items:
                key = ids.get(hit.source.section_id)
                returned.append(key)
                state = check_source(hit.source, hit.excerpt)
                if state != "allowed":
                    valid = False
                    page_acl += int(state == "forbidden")
                    faults.append("SOURCE_" + state.upper())
            if found.reply is not None:
                reply_valid = reply_matches(
                    page,
                    found.reply,
                    allow_handoff=q.negative_behavior == "handoff",
                )
                valid &= reply_valid
        if found and found.reply is not None:
            for source in found.reply.sources:
                state = check_source(source)
                if state != "allowed":
                    reply_acl += int(state == "forbidden")
                    valid = False
                    faults.append("SOURCE_" + state.upper())
        acl_incidents += page_acl + reply_acl
        page_acl_incidents += page_acl
        reply_acl_incidents += reply_acl
        if q.group == "relevant":
            rank = (
                next(
                    (
                        i
                        for i, key in enumerate(returned, 1)
                        if key in q.expected_sections
                    ),
                    None,
                )
                if valid
                else None
            )
            hit_at_5 = int(rank is not None)
            hits += hit_at_5
            if rank:
                reciprocal += Fraction(1, rank)
            safe = valid
        else:
            hit_at_5 = None
            safe = bool(
                valid
                and found
                and found.page is not None
                and (
                    (q.negative_behavior == "no_match" and found.page.total == 0)
                    or (
                        q.negative_behavior == "safe_quote"
                        and returned
                        and set(returned) <= set(q.safe_sections)
                    )
                    or (
                        q.negative_behavior == "handoff"
                        and found.reply is not None
                        and found.reply.resolution == "handoff"
                        and not found.reply.sources
                    )
                )
            )
            negatives += int(safe)
        if not valid:
            faults.append("MISSING_OR_INVALID_RESULT")
        rows.append(
            dict(
                query_id=q.query_id,
                group=q.group,
                subclass=q.subclass,
                expected_section_count=len(q.expected_sections),
                total=found.page.total if found and found.page else None,
                returned_sections=returned,
                first_relevant_position=rank,
                hit_at_5=hit_at_5,
                safe=safe,
                execution_valid=valid,
                reply_valid=reply_valid,
                reply_resolution=found.reply.resolution
                if found and found.reply
                else None,
                http_error=bool(
                    found and (found.error_kind == "http" or not found.http_matches)
                ),
                db_error=bool(found and found.error_kind == "db"),
                query_error=bool(
                    found and (found.error or found.error_kind is not None)
                ),
                result_missing=found is None,
                page_acl_incidents=page_acl,
                reply_acl_incidents=reply_acl,
            )
        )
    denominator = sum(q.group == "relevant" for q in corpus.queries)
    negative_count = len(corpus.queries) - denominator
    quality_pass = hits * 5 >= denominator * 4
    complete = not faults and len(by_id) == len(corpus.queries)
    passed = (
        complete and quality_pass and negatives == negative_count and not acl_incidents
    )
    synthetic = corpus.manifest.status == "synthetic"
    return {
        "format": "kb-acceptance-result-v1",
        "corpus_version": corpus.manifest.corpus_version,
        "corpus_sha256": corpus.checksum,
        "queries_sha256": corpus.manifest.queries_sha256,
        "sections_sha256": corpus.manifest.sections_sha256,
        "corpus_status": corpus.manifest.status,
        "relevant_count": denominator,
        "total_relevant": denominator,
        "negative_count": negative_count,
        "total_negative": negative_count,
        "expected_section_count": sum(len(q.expected_sections) for q in corpus.queries),
        "hits": hits,
        "hit_at_5_count": hits,
        "misses": denominator - hits,
        "miss_at_5_count": denominator - hits,
        "hit_rate_at_5": float(Fraction(hits, denominator)),
        "SYNTHETIC_HIT_RATE_AT_5": float(Fraction(hits, denominator))
        if corpus.manifest.status == "synthetic"
        else "NOT_MEASURED",
        "minimum_hits": (4 * denominator + 4) // 5,
        "mrr_at_5": float(reciprocal / denominator),
        "negative_passed": negatives,
        "negative_safe_count": negatives,
        "negative_unsafe_count": negative_count - negatives,
        "failed_query_ids": [
            row["query_id"]
            for row in rows
            if not row["execution_valid"]
            or row["hit_at_5"] == 0
            or (row["group"] != "relevant" and not row["safe"])
        ],
        "http_error_count": sum(
            o.error_kind == "http" or not o.http_matches for o in observations
        ),
        "db_error_count": sum(o.error_kind == "db" for o in observations),
        "query_error_count": sum(
            o.error or o.error_kind is not None for o in observations
        ),
        "missing_result_count": len({q.query_id for q in corpus.queries} - set(by_id)),
        "measured_corpus_versions": [
            {"version": corpus.manifest.corpus_version, "sha256": corpus.checksum}
        ],
        "acl_incidents": acl_incidents,
        "page_acl_incidents": page_acl_incidents,
        "reply_acl_incidents": reply_acl_incidents,
        "complete": complete,
        "faults": sorted(set(faults)),
        "technical_result": "PASS" if passed else "FAIL",
        "SYNTHETIC_CORPUS_VALIDATION": "PASS" if synthetic and complete else "FAIL",
        "SYNTHETIC_NEGATIVE_SAFETY": "PASS"
        if synthetic and negatives == negative_count and not acl_incidents
        else "FAIL",
        # Only the separate external evidence validator may change real gates.
        "REAL_EXPERT_CORPUS": "BLOCKED",
        "REAL_TOP5_ACCEPTANCE": "BLOCKED",
        "REAL_HIT_RATE_AT_5": "NOT_MEASURED",
        "REAL_NEGATIVE_SAFETY": "BLOCKED",
        "EXPERT_REVIEW": "PENDING",
        "TARGET_STAGING": "BLOCKED",
        "PRODUCTION_ADMISSION": "BLOCKED",
        "queries": rows,
    }
