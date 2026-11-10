"""Measurement integrity tests; these are not FTS relevance evidence."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.chat.mode_router import mode_buttons, specialist
from app.schemas.chat import ChatReply, OpenDocumentAction
from app.schemas.sources import KnowledgeHit, KnowledgePage, SourceRef
from kb_acceptance.corpus import (
    Corpus,
    CorpusRejected,
    canonical,
    load_synthetic,
    sha256,
    validate,
)
from kb_acceptance.external import ExternalPacket, verify_decision
from kb_acceptance.measure import NO_MATCH, Binding, Observation, measure, quoted_text

ROOT = Path(__file__).parent / "fixtures" / "kb_acceptance"


@pytest.fixture
def corpus() -> Corpus:
    return load_synthetic(ROOT)


def sample(corpus: Corpus) -> tuple[dict[str, Binding], list[Observation]]:
    docs = {d.key: uuid4() for d in corpus.manifest.documents}
    versions = {(d.key, d.version): uuid4() for d in corpus.manifest.documents}
    mapping = {}
    for s in corpus.sections:
        did, vid = docs[s.document_key], versions[s.document_key, s.version]
        source = SourceRef(
            section_id=uuid4(),
            document_id=did,
            document_version_id=vid,
            title="SYNTHETIC ONLY",
            section=s.title,
            page_from=s.page_from,
            page_to=s.page_to,
            url=f"/api/v1/documents/{did}/versions/{vid}/file",
        )
        mapping[s.key] = Binding(
            s.document_key, s.version, KnowledgeHit(source=source, excerpt=s.text[:600])
        )
    records = []
    for q in corpus.queries:
        keys = (q.expected_sections if q.group == "relevant" else q.safe_sections)[:1]
        page = KnowledgePage(
            items=[mapping[k].hit for k in keys], total=len(keys), limit=5, offset=0
        )
        records.append(Observation(q.query_id, page, reply(page)))
    return mapping, records


def reply(page: KnowledgePage) -> ChatReply:
    return ChatReply(
        mode="knowledge",
        text=quoted_text(page) if page.items else NO_MATCH,
        buttons=mode_buttons(),
        sources=[h.source for h in page.items],
        items=[],
        actions=[
            OpenDocumentAction(type="open_document", payload=h.source)
            for h in page.items
        ]
        if page.items
        else [specialist()],
        resolution="results" if page.items else "no_match",
    )


def empty(query_id: str) -> Observation:
    page = KnowledgePage(items=[], total=0, limit=5, offset=0)
    return Observation(query_id, page, reply(page))


def validated(
    manifest: dict[str, object], queries: Sequence[object], sections: Sequence[object]
) -> Corpus:
    m = dict(
        manifest,
        queries_sha256=sha256(canonical(queries)),
        sections_sha256=sha256(canonical(sections)),
    )
    return validate(canonical(m), canonical(queries), canonical(sections))


@pytest.mark.parametrize(
    "hits,passed", [(16, True), (15, False), (0, False), (20, True)]
)
def test_exact_threshold_fixed_denominator(
    corpus: Corpus, hits: int, passed: bool
) -> None:
    mapping, records = sample(corpus)
    for index in range(hits, 20):
        records[index] = empty(records[index].query_id)
    result = measure(corpus, mapping, records)
    assert result["hits"] == hits and result["relevant_count"] == 20
    assert result["hit_rate_at_5"] == hits / 20
    assert (result["technical_result"] == "PASS") == passed
    assert result["REAL_EXPERT_CORPUS"] == result["REAL_TOP5_ACCEPTANCE"] == "BLOCKED"
    assert result["REAL_HIT_RATE_AT_5"] == "NOT_MEASURED"


def test_more_than_twenty_never_rounds_up(corpus: Corpus) -> None:
    extra = replace(
        corpus,
        queries=corpus.queries
        + [
            corpus.queries[0].model_copy(
                update={"query_id": "R21", "text": "добавочный вопрос"}
            )
        ],
    )
    mapping, records = sample(extra)
    for index in range(16, 20):
        records[index] = empty(records[index].query_id)
    records[-1] = empty("R21")
    result = measure(extra, mapping, records)
    assert result["minimum_hits"] == 17 and result["technical_result"] == "FAIL"


@pytest.mark.parametrize("fault", ["missing", "error", "duplicate", "unknown", "http"])
def test_incomplete_or_error_is_never_success(corpus: Corpus, fault: str) -> None:
    mapping, records = sample(corpus)
    if fault == "missing":
        records.pop(0)
    elif fault == "duplicate":
        records.append(records[0])
    elif fault == "unknown":
        records.append(empty("UNKNOWN"))
    elif fault == "http":
        records[0] = replace(records[0], http_matches=False)
    else:
        records[0] = Observation(records[0].query_id, error=True)
    result = measure(corpus, mapping, records)
    assert result["relevant_count"] == 20 and result["technical_result"] == "FAIL"
    assert not result["complete"]


@pytest.mark.parametrize(
    "fault",
    [
        "acl",
        "invented",
        "unsafe_quote",
        "unverified_confidence",
        "wrong_source",
        "wrong_excerpt",
    ],
)
def test_safety_overrides_perfect_score(corpus: Corpus, fault: str) -> None:
    mapping, records = sample(corpus)
    index = 20
    if fault in {"acl", "unsafe_quote"}:
        hit = mapping["internal" if fault == "acl" else "indicator"].hit
        page = KnowledgePage(items=[hit], total=1, limit=5, offset=0)
        records[index] = Observation(records[index].query_id, page, reply(page))
    elif fault in {"wrong_source", "wrong_excerpt"}:
        hit = mapping["indicator"].hit
        wrong_document = uuid4()
        bad = (
            hit.model_copy(update={"excerpt": "invented"})
            if fault == "wrong_excerpt"
            else hit.model_copy(
                update={
                    "source": hit.source.model_copy(
                        update={
                            "document_id": wrong_document,
                            "url": f"/api/v1/documents/{wrong_document}/versions/"
                            f"{hit.source.document_version_id}/file",
                        }
                    )
                }
            )
        )
        page = KnowledgePage(items=[bad], total=1, limit=5, offset=0)
        records[0] = Observation("R01", page, reply(page))
    else:
        original_reply = records[index].reply
        assert original_reply is not None
        bad_reply = original_reply.model_copy(
            update={
                "text": "Высокая уверенность"
                if fault == "unverified_confidence"
                else "Выдуманный технический план"
            }
        )
        records[index] = replace(records[index], reply=bad_reply)
    result = measure(corpus, mapping, records)
    assert result["technical_result"] == "FAIL"
    if fault == "acl":
        assert result["hits"] == 20 and result["acl_incidents"] == 2


def test_safe_negative_quote_need_not_have_zero_total(corpus: Corpus) -> None:
    mapping, records = sample(corpus)
    assert records[-1].page is not None and records[-1].page.total == 1
    result = measure(corpus, mapping, records)
    assert result["negative_passed"] == 20 and result["technical_result"] == "PASS"


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate_id",
        "duplicate_text",
        "duplicate_permutation",
        "unknown_section",
        "wrong_version",
        "wrong_document",
        "forbidden_section",
        "checksum",
        "unknown_json_field",
    ],
)
def test_corrupt_corpus_rejected(corpus: Corpus, fault: str) -> None:
    manifest = corpus.manifest.model_dump()
    queries = [q.model_dump() for q in corpus.queries]
    sections = [s.model_dump() for s in corpus.sections]
    if fault == "missing":
        queries.pop()
    elif fault == "duplicate_id":
        queries[1]["query_id"] = queries[0]["query_id"]
    elif fault == "duplicate_text":
        queries[1]["text"] = "  " + str(queries[0]["text"]).upper() + " "
    elif fault == "duplicate_permutation":
        queries[1]["text"] = " ".join(reversed(str(queries[0]["text"]).split()))
    elif fault == "unknown_section":
        queries[0]["expected_sections"] = ["unknown"]
    elif fault == "wrong_version":
        sections[0]["version"] = 99
    elif fault == "wrong_document":
        sections[0]["document_key"] = "unknown"
    elif fault == "forbidden_section":
        queries[0]["expected_sections"] = ["internal"]
    elif fault == "unknown_json_field":
        manifest["approved"] = True
    else:
        with pytest.raises(CorpusRejected):
            validate(canonical(manifest), canonical(queries), canonical(sections))
        return
    with pytest.raises(CorpusRejected):
        validated(manifest, queries, sections)


@pytest.mark.parametrize("fault", ["document", "version", "missing", "section"])
def test_wrong_uuid_mapping_is_rejected(corpus: Corpus, fault: str) -> None:
    mapping, records = sample(corpus)
    if fault == "missing":
        mapping.pop("indicator")
    else:
        binding = mapping["indicator"]
        source = binding.hit.source.model_copy(
            update={
                {
                    "document": "document_id",
                    "version": "document_version_id",
                    "section": "section_id",
                }[fault]: mapping["internal"].hit.source.section_id
                if fault == "section"
                else uuid4()
            }
        )
        mapping["indicator"] = replace(
            binding, hit=binding.hit.model_copy(update={"source": source})
        )
    with pytest.raises(CorpusRejected):
        measure(corpus, mapping, records)


def test_manifest_edit_without_lock_update_detected(tmp_path: Path) -> None:
    for file in ROOT.iterdir():
        (tmp_path / file.name).write_bytes(file.read_bytes())
    path = tmp_path / "manifest.json"
    path.write_text(path.read_text().replace("1.0.0", "1.0.1"))
    with pytest.raises(CorpusRejected, match="MANIFEST_LOCK_MISMATCH"):
        load_synthetic(tmp_path)


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(CorpusRejected):
        validate(b'{"format":1,"format":2}', b"[]", b"[]")


def test_missing_expert_approval_does_not_pass(corpus: Corpus) -> None:
    manifest = corpus.manifest.model_dump()
    manifest["status"] = "expert-approved"
    manifest["queries_sha256"] = sha256(
        canonical([q.model_dump() for q in corpus.queries])
    )
    manifest["sections_sha256"] = sha256(
        canonical([s.model_dump() for s in corpus.sections])
    )
    value = {
        "manifest": manifest,
        "queries": [q.model_dump() for q in corpus.queries],
        "sections": [s.model_dump() for s in corpus.sections],
        "mapping": {},
        "decision": None,
    }
    with pytest.raises(CorpusRejected, match="EXTERNAL_APPROVAL_REQUIRED"):
        verify_decision(ExternalPacket.model_validate(value))


def test_pending_template_and_shipped_fixtures_never_are_evidence(
    corpus: Corpus,
) -> None:
    packet = ExternalPacket(
        manifest=corpus.manifest,
        queries=corpus.queries,
        sections=corpus.sections,
        mapping={},
    )
    with pytest.raises(CorpusRejected):
        verify_decision(packet)


def test_public_projector_no_match_and_handoff_are_checked_exactly(
    corpus: Corpus,
) -> None:
    from app.modules.chat.diagnosis import HANDOFF_TEXT
    from app.modules.chat.handoff import support_handoff
    from kb_acceptance.measure import PROJECTED_NO_MATCH, reply_matches

    page = KnowledgePage(items=[], total=0, limit=5, offset=0)
    projected = reply(page).model_copy(
        update={"text": PROJECTED_NO_MATCH, "actions": [support_handoff()]}
    )
    assert reply_matches(page, projected)
    handoff = projected.model_copy(
        update={"text": HANDOFF_TEXT, "resolution": "handoff", "buttons": []}
    )
    assert reply_matches(page, handoff, allow_handoff=True)
    assert not reply_matches(page, handoff)
    assert not reply_matches(
        page,
        handoff.model_copy(update={"text": "unverified instruction"}),
        allow_handoff=True,
    )


def test_synthetic_review_cannot_be_relabelled_as_real_approval(corpus: Corpus) -> None:
    from kb_acceptance.external import ExpertDecision, Link

    queries = [
        q.model_copy(update={"expert_status": "approved"}) for q in corpus.queries
    ]
    mapping, _ = sample(corpus)
    links = {
        k: Link(
            document_code="SYNTHETIC-ONLY-" + b.document_key,
            document_id=str(b.hit.source.document_id),
            document_version_id=str(b.hit.source.document_version_id),
            section_id=str(b.hit.source.section_id),
        )
        for k, b in mapping.items()
    }
    approval = corpus.manifest.expert_approval.model_copy(
        update={
            "status": "approved",
            "reference": "SYNTHETIC-ONLY:REVIEW",
            "rights_reference": "SYNTHETIC-ONLY:RIGHTS",
            "reviewer": "SYNTHETIC ONLY Expert fixture",
            "approved_on": "2026-01-01",
        }
    )
    manifest = corpus.manifest.model_copy(
        update={
            "status": "expert-approved",
            "expert_approval": approval,
            "queries_sha256": sha256(canonical([q.model_dump() for q in queries])),
            "sections_sha256": sha256(
                canonical([s.model_dump() for s in corpus.sections])
            ),
        }
    )
    decision = ExpertDecision(
        decision="APPROVED",
        corpus_sha256=sha256(canonical(manifest.model_dump())),
        mapping_sha256=sha256(canonical({k: v.model_dump() for k, v in links.items()})),
        relevant_query_ids=[q.query_id for q in queries if q.group == "relevant"],
        negative_query_ids=[q.query_id for q in queries if q.group != "relevant"],
        expected_sections={
            q.query_id: q.expected_sections for q in queries if q.group == "relevant"
        },
        reference="SYNTHETIC-ONLY:REVIEW",
        rights_reference="SYNTHETIC-ONLY:RIGHTS",
        reviewer="SYNTHETIC ONLY Expert fixture",
        approved_on="2026-01-01",
        quote_boundaries_checked=True,
        no_unverified_instructions=True,
        full_file_public_allowed=True,
    )
    packet = ExternalPacket(
        manifest=manifest,
        queries=queries,
        sections=corpus.sections,
        mapping=links,
        decision=decision,
    )
    with pytest.raises(CorpusRejected, match="SYNTHETIC_IS_NOT_REAL_EVIDENCE"):
        verify_decision(packet)


@pytest.mark.parametrize("kind", ["db", "http"])
def test_error_counters_and_fixed_denominator(corpus: Corpus, kind: str) -> None:
    mapping, records = sample(corpus)
    records[0] = Observation("R01", error=True, error_kind=kind)  # type: ignore[arg-type]
    result = measure(corpus, mapping, records)
    assert result["total_relevant"] == 20
    assert result["hit_at_5_count"] == 19
    assert result["miss_at_5_count"] == 1
    assert result["total_negative"] == result["negative_safe_count"] == 20
    assert result["negative_unsafe_count"] == 0
    assert result[f"{kind}_error_count"] == result["query_error_count"] == 1
    assert result["failed_query_ids"] == ["R01"]
    assert result["technical_result"] == "FAIL"


@pytest.mark.parametrize(
    "placement", ["before", "after", "instead", "no_match", "safe_quote"]
)
def test_verified_public_competitor_preserves_positions_and_query_safety(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    from unittest.mock import Mock

    from sqlalchemy.orm import Session

    mapping, records = sample(corpus)
    competitor = mapping["indicator"].hit.model_copy(
        update={
            "source": mapping["indicator"].hit.source.model_copy(
                update={"section_id": uuid4()}
            )
        }
    )
    monkeypatch.setattr(
        "kb_acceptance.measure.verify_public_source", lambda *_: "allowed"
    )
    index = 20 if placement == "no_match" else 39 if placement == "safe_quote" else 0
    expected = mapping["safety" if placement == "safe_quote" else "indicator"].hit
    items = (
        [competitor, expected]
        if placement == "before"
        else [expected, competitor]
        if placement in {"after", "safe_quote"}
        else [competitor]
    )
    page = KnowledgePage(items=items, total=len(items), limit=5, offset=0)
    records[index] = Observation(records[index].query_id, page, reply(page))
    result = measure(corpus, mapping, records, session=Mock(spec=Session))
    rows = result["queries"]
    assert isinstance(rows, list)
    row = rows[index]
    assert row["execution_valid"] and result["acl_incidents"] == 0
    if index == 0:
        rank = {"before": 2, "after": 1, "instead": None}[placement]
        assert row["first_relevant_position"] == rank
        assert row["hit_at_5"] == int(rank is not None)
        assert result["hits"] == (19 if rank is None else 20)
        assert result["mrr_at_5"] == (19 + (1 / rank if rank else 0)) / 20
    else:
        assert not row["safe"] and result["negative_passed"] == 19


def test_unknown_source_without_independent_evidence_fails_closed(
    corpus: Corpus,
) -> None:
    mapping, records = sample(corpus)
    hit = mapping["indicator"].hit
    hit = hit.model_copy(
        update={"source": hit.source.model_copy(update={"section_id": uuid4()})}
    )
    page = KnowledgePage(items=[hit], total=1, limit=5, offset=0)
    records[0] = Observation("R01", page, reply(page))
    result = measure(corpus, mapping, records)
    assert result["technical_result"] == "FAIL" and result["acl_incidents"] == 0
    faults = result["faults"]
    assert isinstance(faults, list) and "SOURCE_UNVERIFIED" in faults
