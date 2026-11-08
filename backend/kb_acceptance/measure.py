"""Section-level hit@5 with a fixed denominator and independent safety gates."""

from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from app.modules.chat.diagnosis import HANDOFF_TEXT
from app.modules.chat.handoff import support_handoff
from app.modules.chat.mode_router import mode_buttons, specialist
from app.schemas.chat import ChatReply
from app.schemas.sources import KnowledgeHit, KnowledgePage
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


def measure(
    corpus: Corpus,
    mapping: dict[str, Binding],
    observations: list[Observation],
) -> dict[str, object]:
    check_mapping(corpus, mapping)
    ids = {b.hit.source.section_id: key for key, b in mapping.items()}
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
    reciprocal = Fraction(0)
    for q in corpus.queries:
        found = by_id.get(q.query_id)
        valid = bool(
            found
            and not found.error
            and found.http_matches
            and found.page is not None
            and found.reply is not None
        )
        rank = None
        returned: list[str] = []
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
                if key is None or not corpus.admissible(key):
                    acl_incidents += 1
                    valid = False
                elif hit != mapping[key].hit:
                    valid = False
                    faults.append("SOURCE_OR_EXCERPT_MISMATCH")
                else:
                    returned.append(key)
            if found.reply is not None:
                valid &= reply_matches(
                    page,
                    found.reply,
                    allow_handoff=q.negative_behavior == "handoff",
                )
                if any(
                    s.section_id not in ids or not corpus.admissible(ids[s.section_id])
                    for s in found.reply.sources
                ):
                    acl_incidents += 1
                    valid = False
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
        "negative_count": negative_count,
        "expected_section_count": sum(len(q.expected_sections) for q in corpus.queries),
        "hits": hits,
        "misses": denominator - hits,
        "hit_rate_at_5": float(Fraction(hits, denominator)),
        "SYNTHETIC_HIT_RATE_AT_5": float(Fraction(hits, denominator))
        if corpus.manifest.status == "synthetic"
        else "NOT_MEASURED",
        "minimum_hits": (4 * denominator + 4) // 5,
        "mrr_at_5": float(reciprocal / denominator),
        "negative_passed": negatives,
        "acl_incidents": acl_incidents,
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
