"""Tests for blackboard init node and helpers."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from fp2mp_core.nodes.blackboard import (
    coverage_from_sub_queries,
    wiki_briefing,
)
from fp2mp_core.state import ConfirmedFact, SubQuery, create_initial_state


def test_coverage_returns_pending_when_no_output(sub_queries):
    state = create_initial_state("test question")
    state["redi_decomposition"] = sub_queries
    coverage = coverage_from_sub_queries(state)
    assert coverage["sq_001"] == "pending"
    assert coverage["sq_002"] == "pending"


def test_coverage_returns_covered_when_high_confidence_fact(sub_queries):
    state = create_initial_state("test question")
    state["redi_decomposition"] = sub_queries
    state["output"] = [
        ConfirmedFact(
            fact_id="f1", claim="Some claim", source_agents=["A"],
            confidence=0.75, citations=[], limitations=[], sub_query_id="sq_001"
        )
    ]
    coverage = coverage_from_sub_queries(state)
    assert coverage["sq_001"] == "covered"
    assert coverage["sq_002"] == "pending"


def test_coverage_returns_partial_when_low_confidence(sub_queries):
    state = create_initial_state("test question")
    state["redi_decomposition"] = sub_queries
    state["output"] = [
        ConfirmedFact(
            fact_id="f1", claim="Some claim", source_agents=["A"],
            confidence=0.55, citations=[], limitations=[], sub_query_id="sq_001"
        )
    ]
    coverage = coverage_from_sub_queries(state)
    assert coverage["sq_001"] == "partial"


def test_wiki_briefing_includes_index(wiki_pages):
    state = create_initial_state("test")
    state["wiki"] = wiki_pages
    brief = wiki_briefing(state)
    assert "Knowledge Index" in brief or "index" in brief.lower()


def test_wiki_briefing_includes_confirmed_facts():
    state = create_initial_state("test")
    state["output"] = [
        ConfirmedFact(
            fact_id="f1", claim="Important fact about airports.",
            source_agents=["NormativeAgent"], confidence=0.85,
            citations=[], limitations=[], sub_query_id="sq_001"
        )
    ]
    brief = wiki_briefing(state)
    assert "Important fact" in brief


def test_initialize_blackboard_creates_wiki_skeleton(sub_queries):
    """integration: init node creates index.md and log.md."""
    with patch("fp2mp_core.nodes.blackboard.ReDIDecomposer"), \
         patch("fp2mp_core.nodes.blackboard.ReDIEnricher"):

        from fp2mp_core.nodes.blackboard import initialize_blackboard_node
        state = create_initial_state("test question")
        state["redi_decomposition"] = sub_queries

        result = initialize_blackboard_node(state)

        assert "index.md" in result["wiki"]
        assert "log.md" in result["wiki"]
        assert len(result["tasks"]) == len(sub_queries)
