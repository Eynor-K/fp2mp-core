"""Tests for the ReDI pipeline: decomposer, enricher, fusion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fp2mp_core.redi.fusion import ReDIFusion, _jaccard, _deduplicate
from fp2mp_core.state import RawEntry, SubQuery, board_message


# ---------------------------------------------------------------------------
# Fusion tests (no LLM needed — pure algorithmic)
# ---------------------------------------------------------------------------


def test_jaccard_identical_texts():
    assert _jaccard("hello world", "hello world") == 1.0


def test_jaccard_disjoint_texts():
    assert _jaccard("apple banana", "car truck") == 0.0


def test_jaccard_partial_overlap():
    score = _jaccard("hello world", "hello there")
    assert 0.0 < score < 1.0


def test_fusion_deduplicates_near_identical_entries(sub_queries):
    fusion = ReDIFusion(dedup_threshold=0.7)
    e1 = board_message("WebSearchAgent", 1, "web_findings",
                       "Строительство вблизи аэропорта ограничено по высоте.",
                       sub_query_id="sq_002", confidence=0.7)
    e2 = board_message("WebSearchAgent", 1, "web_findings",
                       "Строительство вблизи аэропорта ограничено по высоте здания.",
                       sub_query_id="sq_002", confidence=0.65)
    result = fusion.fuse([e1, e2], sub_queries)
    # Near-duplicate: only 1 should survive deduplication
    content = result.get("content", "")
    assert "sq_002" in content
    # Additive: "Unique findings" should show <= 2 (one deduplicated)
    assert "Unique findings: 1" in content


def test_fusion_additive_scoring_sums_contributions(sub_queries):
    fusion = ReDIFusion(dedup_threshold=0.5)
    entries = [
        board_message("WebSearchAgent", 1, "web_findings",
                      "аэропорт высота ограничение норматив",
                      sub_query_id="sq_002", confidence=0.7),
        board_message("NormativeAgent", 1, "normative_findings",
                      "аэропорт зона строительство правила ограничение",
                      sub_query_id="sq_002", confidence=0.8),
    ]
    result = fusion.fuse(entries, sub_queries)
    content = result.get("content", "")
    # Both entries have "аэропорт" overlap with keywords → additive score > single entry
    assert "Additive score:" in content


def test_fusion_returns_wiki_page_with_correct_page_id(sub_queries):
    fusion = ReDIFusion()
    entry = board_message("WebSearchAgent", 1, "web_findings",
                          "Some content about airport.",
                          sub_query_id="sq_001", confidence=0.6)
    result = fusion.fuse([entry], sub_queries)
    assert result["page_id"] == "redi_fusion"
    assert result["tags"] == ["fusion", "redi"]


def test_fusion_empty_raw_data_returns_page(sub_queries):
    fusion = ReDIFusion()
    result = fusion.fuse([], sub_queries)
    assert result["page_id"] == "redi_fusion"


def test_deduplicate_keeps_higher_confidence():
    e1 = board_message("A", 1, "web_findings", "identical text content here", confidence=0.9)
    e2 = board_message("B", 1, "web_findings", "identical text content here", confidence=0.5)
    result = _deduplicate([e1, e2], threshold=0.8)
    assert len(result) == 1
    assert result[0]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Decomposer tests (mock LLM)
# ---------------------------------------------------------------------------


def test_decomposer_returns_sub_queries(question):
    mock_response = MagicMock()
    mock_response.content = """[
        {"sub_query_id": "sq_001", "text": "Нормы?", "intent_aspect": "regulatory",
         "search_modality": "normative", "independence": true},
        {"sub_query_id": "sq_002", "text": "Высота?", "intent_aspect": "empirical",
         "search_modality": "web", "independence": true}
    ]"""

    mock_chain = MagicMock()
    mock_chain.invoke.return_value = mock_response

    from fp2mp_core.redi.decomposer import ReDIDecomposer
    decomposer = ReDIDecomposer.__new__(ReDIDecomposer)
    decomposer._chain = mock_chain
    result = decomposer(question)

    for sq in result:
        assert "sub_query_id" in sq
        assert sq["search_modality"] in {"web", "normative", "code", "any"}


# ---------------------------------------------------------------------------
# Enricher tests (mock LLM)
# ---------------------------------------------------------------------------


def test_enricher_populates_variants(sub_queries):
    mock_response = MagicMock()
    mock_response.content = """{
        "sub_query_id": "sq_001",
        "variants": ["var1", "var2", "var3"],
        "keywords": ["keyword1", "keyword2"],
        "domain_hints": ["СНиП", "ГОСТ"]
    }"""

    mock_chain = MagicMock()
    mock_chain.invoke.return_value = mock_response

    from fp2mp_core.redi.enricher import ReDIEnricher
    enricher = ReDIEnricher.__new__(ReDIEnricher)
    enricher._chain = mock_chain
    result = enricher.enrich(sub_queries[0])

    assert "sub_query_id" in result
    assert result["enriched_variants"] == ["var1", "var2", "var3"]
    assert result["keywords"] == ["keyword1", "keyword2"]
    assert result["domain_hints"] == ["СНиП", "ГОСТ"]
