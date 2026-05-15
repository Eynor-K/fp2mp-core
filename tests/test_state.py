"""Tests for state.py: data contracts and reducer functions."""

from __future__ import annotations

from fp2mp_core.nodes.blackboard import init_node
from fp2mp_core.state import (
    ConfirmedFact,
    RawEntry,
    Task,
    WikiPage,
    _merge_output,
    _merge_tasks,
    _merge_wiki,
    board_message,
    create_initial_state,
)


def test_create_initial_state_is_minimal(question):
    state = create_initial_state(question)
    assert state["question"] == question
    assert state["max_iterations"] is None


def test_init_node_sets_graph_defaults(question):
    state = create_initial_state(question)
    result = init_node(state)
    assert result["raw_data"] == []
    assert result["wiki"] == {}
    assert result["output"] == []
    assert result["iteration"] == 0
    assert result["stop_flag"] is False
    assert result["max_iterations"] == 6


def test_merge_wiki_upserts_by_page_id():
    page_a = WikiPage(page_id="p1", title="A", content="old", confidence=0.5,
                      updated_by="x", updated_at_iteration=0, citations=[],
                      tags=[], incoming_cross_refs=[], relevance_score=0.0)
    page_b = WikiPage(page_id="p1", title="A", content="new", confidence=0.8,
                      updated_by="y", updated_at_iteration=1, citations=[],
                      tags=[], incoming_cross_refs=[], relevance_score=0.0)
    result = _merge_wiki({"p1": page_a}, {"p1": page_b})
    assert result["p1"]["content"] == "new"
    assert result["p1"]["confidence"] == 0.8


def test_merge_wiki_preserves_left_keys_not_in_right():
    page_a = WikiPage(page_id="p1", title="A", content="x", confidence=0.5,
                      updated_by="a", updated_at_iteration=0, citations=[],
                      tags=[], incoming_cross_refs=[], relevance_score=0.0)
    page_b = WikiPage(page_id="p2", title="B", content="y", confidence=0.6,
                      updated_by="b", updated_at_iteration=0, citations=[],
                      tags=[], incoming_cross_refs=[], relevance_score=0.0)
    result = _merge_wiki({"p1": page_a}, {"p2": page_b})
    assert "p1" in result
    assert "p2" in result


def test_merge_output_keeps_max_confidence():
    fact_low = ConfirmedFact(
        fact_id="f1", claim="claim", source_agents=["A"], confidence=0.5,
        citations=[], limitations=[], sub_query_id="sq_001"
    )
    fact_high = ConfirmedFact(
        fact_id="f1", claim="claim", source_agents=["B"], confidence=0.9,
        citations=[], limitations=[], sub_query_id="sq_001"
    )
    result = _merge_output([fact_low], [fact_high])
    assert len(result) == 1
    assert result[0]["confidence"] == 0.9


def test_merge_output_does_not_duplicate():
    fact = ConfirmedFact(
        fact_id="f1", claim="claim", source_agents=["A"], confidence=0.7,
        citations=[], limitations=[], sub_query_id="sq_001"
    )
    result = _merge_output([fact], [fact])
    assert len(result) == 1


def test_merge_tasks_upserts_by_task_id():
    t1 = Task(task_id="t1", sub_query_id="sq_001", assigned_agent="WebSearchAgent",
               status="pending", priority="high", proposer="system",
               created_at_iteration=0, directive="")
    t1_updated = Task(task_id="t1", sub_query_id="sq_001", assigned_agent="WebSearchAgent",
                      status="done", priority="high", proposer="system",
                      created_at_iteration=0, directive="")
    result = _merge_tasks([t1], [t1_updated])
    assert len(result) == 1
    assert result[0]["status"] == "done"


def test_board_message_sets_curated_false():
    entry = board_message(
        agent="TestAgent", iteration=1, msg_type="test",
        content="hello", sub_query_id="sq_001", confidence=0.8
    )
    assert entry["curated"] is False
    assert entry["agent"] == "TestAgent"
    assert entry["confidence"] == 0.8
    assert "entry_id" in entry


def test_merge_output_lower_confidence_does_not_replace():
    fact_high = ConfirmedFact(
        fact_id="f1", claim="claim", source_agents=["A"], confidence=0.9,
        citations=[], limitations=[], sub_query_id="sq_001"
    )
    fact_low = ConfirmedFact(
        fact_id="f1", claim="claim updated", source_agents=["B"], confidence=0.3,
        citations=[], limitations=[], sub_query_id="sq_001"
    )
    result = _merge_output([fact_high], [fact_low])
    assert result[0]["confidence"] == 0.9
