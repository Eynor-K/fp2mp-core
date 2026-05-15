"""Tests for LLM-Wiki: page building, maintenance (prune/merge), and index."""

from __future__ import annotations

from fp2mp_core.state import RawEntry, SubQuery, WikiPage, board_message
from fp2mp_core.wiki.index import build_index, parse_index
from fp2mp_core.wiki.log import append_log_entry, make_log_entry
from fp2mp_core.wiki.maintenance import (
    compute_relevance_scores,
    merge_overlapping_pages,
    prune_wiki,
)
from fp2mp_core.wiki.page import WikiPageBuilder, update_incoming_cross_refs


# ---------------------------------------------------------------------------
# WikiPageBuilder
# ---------------------------------------------------------------------------


def test_page_builder_creates_page_from_entry(sub_queries, raw_entries):
    builder = WikiPageBuilder()
    page = builder.build(raw_entries[0], sub_queries[1], {}, iteration=1)
    assert page["page_id"] == "websearch_sq_002"
    assert "аэропорт" in page["content"] or "ограничен" in page["content"]
    assert page["confidence"] == 0.7


def test_page_builder_detects_conflict():
    builder = WikiPageBuilder()
    entry1 = board_message("A", 1, "web_findings",
                           "строительство разрешено вблизи аэропорта без ограничений",
                           confidence=0.6)
    entry2 = board_message("B", 2, "normative_findings",
                           "турбина двигатель велосипед мотор шасси крыло фюзеляж навигация",
                           confidence=0.7)
    page1 = WikiPage(
        page_id="test_page", title="Test", content=entry1["content"],
        updated_by="A", updated_at_iteration=1, confidence=0.6,
        citations=[], tags=[], incoming_cross_refs=[], relevance_score=0.0
    )
    page2 = builder.update_page(page1, entry2, {"test_page": page1}, iteration=2)
    assert "> CONFLICT" in page2["content"]


def test_page_builder_injects_cross_refs():
    builder = WikiPageBuilder()
    existing_page = WikiPage(
        page_id="normative_sq001", title="Normative findings about airport",
        content="regulatory info", updated_by="NormativeAgent",
        updated_at_iteration=1, confidence=0.8, citations=[],
        tags=["normative"], incoming_cross_refs=[], relevance_score=0.0
    )
    # Entry mentions the exact title of existing page
    entry = board_message("WebSearchAgent", 2, "web_findings",
                          "Normative findings about airport suggests high confidence.",
                          sub_query_id="sq_002", confidence=0.7)
    page = builder.build(entry, None, {"normative_sq001": existing_page}, iteration=2)
    assert "[[normative_sq001]]" in page["content"]


def test_update_incoming_cross_refs():
    wiki = {
        "page_a": WikiPage(
            page_id="page_a", title="A", content="See [[page_b]] for details.",
            updated_by="x", updated_at_iteration=1, confidence=0.5,
            citations=[], tags=[], incoming_cross_refs=[], relevance_score=0.0
        ),
        "page_b": WikiPage(
            page_id="page_b", title="B", content="B content.",
            updated_by="y", updated_at_iteration=1, confidence=0.6,
            citations=[], tags=[], incoming_cross_refs=[], relevance_score=0.0
        ),
    }
    updated = update_incoming_cross_refs(wiki)
    assert "page_a" in updated["page_b"]["incoming_cross_refs"]


# ---------------------------------------------------------------------------
# Maintenance: Pruning
# ---------------------------------------------------------------------------


def test_prune_removes_low_conf_isolated_irrelevant_page(sub_queries):
    wiki = {
        "index.md": WikiPage(page_id="index.md", title="Index", content="",
                              updated_by="sys", updated_at_iteration=0, confidence=1.0,
                              citations=[], tags=["system"], incoming_cross_refs=[],
                              relevance_score=1.0),
        "junk_page": WikiPage(page_id="junk_page", title="Irrelevant data",
                              content="totally unrelated content",
                              updated_by="A", updated_at_iteration=0, confidence=0.2,
                              citations=[], tags=["other_domain"],
                              incoming_cross_refs=[], relevance_score=0.0),
    }
    result = prune_wiki(wiki, sub_queries, current_iteration=3)
    assert "junk_page" not in result
    assert "index.md" in result  # protected


def test_prune_does_not_remove_recent_pages(sub_queries):
    wiki = {
        "new_page": WikiPage(page_id="new_page", title="New", content="new content",
                              updated_by="A", updated_at_iteration=2, confidence=0.2,
                              citations=[], tags=[], incoming_cross_refs=[],
                              relevance_score=0.0),
    }
    result = prune_wiki(wiki, sub_queries, current_iteration=3)
    # age = 3-2 = 1, which is < _PRUNE_MIN_ITERATIONS=2 → should NOT be pruned
    assert "new_page" in result


def test_prune_keeps_pages_with_cross_refs(sub_queries):
    wiki = {
        "low_conf_page": WikiPage(
            page_id="low_conf_page", title="Low", content="content",
            updated_by="A", updated_at_iteration=0, confidence=0.2,
            citations=[], tags=[], incoming_cross_refs=["other_page"],
            relevance_score=0.5
        ),
    }
    result = prune_wiki(wiki, sub_queries, current_iteration=5)
    # Has incoming cross-refs → should NOT be pruned
    assert "low_conf_page" in result


# ---------------------------------------------------------------------------
# Maintenance: Merging
# ---------------------------------------------------------------------------


def test_merge_combines_similar_pages():
    wiki = {
        "page_a": WikiPage(
            page_id="page_a", title="Airport height limits",
            content="аэропорт высота ограничение строительство норматив регулирование",
            updated_by="A", updated_at_iteration=1, confidence=0.7,
            citations=[{"url": "url_a"}], tags=["web"],
            incoming_cross_refs=[], relevance_score=0.5
        ),
        "page_b": WikiPage(
            page_id="page_b", title="Airport height limits duplicate",
            content="аэропорт высота ограничение строительство норматив регулирование закон",
            updated_by="B", updated_at_iteration=1, confidence=0.6,
            citations=[{"url": "url_b"}], tags=["normative"],
            incoming_cross_refs=[], relevance_score=0.4
        ),
    }
    result, merge_log = merge_overlapping_pages(wiki, iteration=2)
    assert len(result) == 1
    assert len(merge_log) == 1
    merged = list(result.values())[0]
    assert merged["confidence"] == 0.7  # max


def test_merge_deduplicates_citations():
    wiki = {
        "page_a": WikiPage(
            page_id="page_a", title="Page A",
            content="аэропорт высота ограничение строительство норматив регулирование правило",
            updated_by="A", updated_at_iteration=1, confidence=0.7,
            citations=[{"url": "https://shared.com"}, {"url": "https://only_a.com"}],
            tags=[], incoming_cross_refs=[], relevance_score=0.5
        ),
        "page_b": WikiPage(
            page_id="page_b", title="Page B",
            content="аэропорт высота ограничение строительство норматив регулирование закон",
            updated_by="B", updated_at_iteration=1, confidence=0.6,
            citations=[{"url": "https://shared.com"}, {"url": "https://only_b.com"}],
            tags=[], incoming_cross_refs=[], relevance_score=0.4
        ),
    }
    result, _ = merge_overlapping_pages(wiki, iteration=2)
    merged = list(result.values())[0]
    # https://shared.com should appear only once
    urls = [c.get("url", "") for c in merged.get("citations", [])]
    assert urls.count("https://shared.com") == 1


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def test_compute_relevance_scores_higher_for_sq_match(sub_queries):
    wiki = {
        "matching_page": WikiPage(
            page_id="matching_page", title="Matching",
            content="content", updated_by="A", updated_at_iteration=1,
            confidence=0.7, citations=[], tags=["regulatory constraint"],  # matches intent_aspect
            incoming_cross_refs=["other"], relevance_score=0.0
        ),
        "nonmatching_page": WikiPage(
            page_id="nonmatching_page", title="Non-matching",
            content="content", updated_by="B", updated_at_iteration=1,
            confidence=0.7, citations=[], tags=["unrelated_domain"],
            incoming_cross_refs=["other"], relevance_score=0.0
        ),
    }
    result = compute_relevance_scores(wiki, sub_queries)
    assert result["matching_page"]["relevance_score"] > result["nonmatching_page"]["relevance_score"]


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def test_build_and_parse_index_round_trip():
    wiki = {
        "page1": WikiPage(
            page_id="page1", title="Test Page", content="content",
            updated_by="AgentA", updated_at_iteration=2, confidence=0.75,
            citations=[], tags=[], incoming_cross_refs=[], relevance_score=1.2
        ),
    }
    index_content = build_index(wiki, iteration=2)
    assert "page1" in index_content
    assert "Test Page" in index_content
    assert "0.75" in index_content

    parsed = parse_index(index_content)
    assert "page1" in parsed
    assert abs(parsed["page1"]["confidence"] - 0.75) < 0.01


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


def test_append_log_entry_is_append_only():
    entry = make_log_entry(1, "WikiCurator", "page_created", "test_page", "Summary text")
    log1 = append_log_entry("# Change Log\n", entry)
    log2 = append_log_entry(log1, entry)
    assert log2.count("page_created") == 2
    assert log2.startswith("# Change Log")
