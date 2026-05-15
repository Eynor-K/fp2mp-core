"""Tests for WikiCuratorAgent node."""

from __future__ import annotations

from fp2mp_core.nodes.curator import route_from_curator, wiki_curator_node
from fp2mp_core.state import ConfirmedFact, WikiPage, board_message, create_initial_state


def _state_with_entries(sub_queries, raw_entries, iteration=1, max_iter=6):
    state = create_initial_state("test?", max_iterations=max_iter)
    state["redi_decomposition"] = sub_queries
    state["raw_data"] = raw_entries
    state["iteration"] = iteration
    state["wiki"] = {
        "index.md": WikiPage(page_id="index.md", title="Index", content="# Index\n",
                              updated_by="system", updated_at_iteration=0, confidence=1.0,
                              citations=[], tags=["system"], incoming_cross_refs=[],
                              relevance_score=1.0),
        "log.md": WikiPage(page_id="log.md", title="Log", content="# Change Log\n",
                            updated_by="system", updated_at_iteration=0, confidence=1.0,
                            citations=[], tags=["system"], incoming_cross_refs=[],
                            relevance_score=1.0),
    }
    return state


def test_curator_creates_wiki_pages_from_entries(sub_queries, raw_entries):
    state = _state_with_entries(sub_queries, raw_entries)
    result = wiki_curator_node(state)
    wiki = result["wiki"]
    # Should have pages for both entries + index + log + redi_fusion
    data_pages = {k: v for k, v in wiki.items() if k not in {"index.md", "log.md", "redi_fusion"}}
    assert len(data_pages) >= 1


def test_curator_promotes_high_confidence_to_output(sub_queries, raw_entries):
    # raw_entries has confidence 0.7 and 0.85
    state = _state_with_entries(sub_queries, raw_entries)
    result = wiki_curator_node(state)
    # Both entries above threshold (0.65 / 0.7 for normative) → should promote
    assert len(result["output"]) >= 1


def test_curator_does_not_promote_below_threshold(sub_queries):
    low_conf = board_message(
        "WebSearchAgent", 1, "web_findings",
        "Some content that is longer than 50 characters so the length check passes.",
        sub_query_id="sq_002", confidence=0.4  # below 0.65
    )
    state = _state_with_entries(sub_queries, [low_conf])
    result = wiki_curator_node(state)
    assert len(result["output"]) == 0


def test_curator_updates_log_md(sub_queries, raw_entries):
    state = _state_with_entries(sub_queries, raw_entries)
    result = wiki_curator_node(state)
    log_content = result["wiki"]["log.md"]["content"]
    assert "page_created" in log_content or "page_updated" in log_content


def test_curator_updates_index_md(sub_queries, raw_entries):
    state = _state_with_entries(sub_queries, raw_entries)
    result = wiki_curator_node(state)
    index_content = result["wiki"]["index.md"]["content"]
    assert "| " in index_content  # table format


def test_curator_increments_iteration(sub_queries, raw_entries):
    state = _state_with_entries(sub_queries, raw_entries, iteration=2)
    result = wiki_curator_node(state)
    assert result["iteration"] == 3


def test_curator_detects_stagnation_when_no_entries(sub_queries):
    state = _state_with_entries(sub_queries, [], iteration=1)
    state["stagnation_count"] = 1
    result = wiki_curator_node(state)
    assert result["stagnation_count"] == 2


def test_curator_resets_stagnation_when_new_entries(sub_queries, raw_entries):
    state = _state_with_entries(sub_queries, raw_entries, iteration=1)
    state["stagnation_count"] = 1
    result = wiki_curator_node(state)
    assert result["stagnation_count"] == 0


def test_route_from_curator_returns_critic_when_facts_exist(sub_queries):
    state = create_initial_state("test", max_iterations=6)
    state["output"] = [
        ConfirmedFact(fact_id="f1", claim="x", source_agents=["A"],
                      confidence=0.8, citations=[], limitations=[], sub_query_id="sq_001")
    ]
    state["iteration"] = 2
    assert route_from_curator(state) == "critic"


def test_route_from_curator_returns_finish_ready_at_max_iter(sub_queries):
    state = create_initial_state("test", max_iterations=3)
    state["iteration"] = 3
    assert route_from_curator(state) == "finish_ready"


def test_route_from_curator_returns_continue_when_no_facts():
    state = create_initial_state("test", max_iterations=6)
    state["output"] = []
    state["iteration"] = 1
    assert route_from_curator(state) == "continue"
