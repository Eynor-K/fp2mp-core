"""Tests for CriticAgent STOP/CONTINUE logic."""

from __future__ import annotations

from unittest.mock import patch

from fp2mp_core.nodes.critic import critic_node, route_from_critic
from fp2mp_core.state import ConfirmedFact, create_initial_state


def _state_with_high_conf_facts(sub_queries, question="test?"):
    state = create_initial_state(question, max_iterations=6)
    state["redi_decomposition"] = sub_queries
    state["output"] = [
        ConfirmedFact(
            fact_id=f"f{i}", claim=f"Claim {i}", source_agents=["A"],
            confidence=0.8, citations=[], limitations=[],
            sub_query_id=sq["sub_query_id"]
        )
        for i, sq in enumerate(sub_queries)
    ]
    return state


def test_critic_force_stops_at_max_iterations(sub_queries):
    state = create_initial_state("test", max_iterations=3)
    state["redi_decomposition"] = sub_queries
    state["iteration"] = 3  # at limit

    result = critic_node(state)
    assert result["stop_flag"] is True
    assert result["critique"]["force_stop"] is True


def test_critic_force_stops_on_stagnation(sub_queries):
    state = create_initial_state("test", max_iterations=6)
    state["redi_decomposition"] = sub_queries
    state["stagnation_count"] = 2

    result = critic_node(state)
    assert result["stop_flag"] is True
    assert "stagnation" in result["critique"]["reasoning"].lower()


def test_critic_stops_when_covered_and_integrative_answer_present(sub_queries):
    """P2.2 holistic contract: covered sub-queries STOP only when an
    integrative answer/recommendation for the original question exists."""
    state = _state_with_high_conf_facts(sub_queries)
    state["output"].append(
        ConfirmedFact(
            fact_id="rec", claim="Recommended option X", source_agents=["Mediator"],
            confidence=0.8, citations=[], limitations=[],
            sub_query_id=sub_queries[0]["sub_query_id"], is_recommendation=True,
        )
    )
    result = critic_node(state)
    assert result["stop_flag"] is True
    assert result["critique"]["action"] == "STOP"


def test_critic_continues_when_covered_but_no_integrative_answer(sub_queries):
    """P2.2: per-sub-query coverage alone is NOT a STOP reason."""
    state = _state_with_high_conf_facts(sub_queries)  # facts, but no recommendation
    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking...",
            '{"action": "STOP", "question_answered": false, '
            '"overall_confidence": 0.8, "reasoning": "covered", '
            '"new_tasks": [], "contradictions": []}'
        )
        result = critic_node(state)
    assert result["critique"]["action"] == "CONTINUE"
    assert result["stop_flag"] is False


def test_critic_continues_when_no_output(sub_queries):
    state = create_initial_state("test", max_iterations=6)
    state["redi_decomposition"] = sub_queries
    # No output facts

    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking...",
            '{"action": "CONTINUE", "overall_confidence": 0.3, '
            '"reasoning": "No facts yet.", "new_tasks": [], "contradictions": []}'
        )
        result = critic_node(state)

    assert result["stop_flag"] is False
    assert result["critique"]["action"] == "CONTINUE"


def test_critic_continues_when_partial_coverage(sub_queries):
    state = create_initial_state("test", max_iterations=6)
    state["redi_decomposition"] = sub_queries
    # Only sq_001 covered
    state["output"] = [
        ConfirmedFact(
            fact_id="f1", claim="Claim", source_agents=["A"],
            confidence=0.8, citations=[], limitations=[], sub_query_id="sq_001"
        )
    ]

    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking...",
            '{"action": "CONTINUE", "overall_confidence": 0.5, '
            '"reasoning": "sq_002 still pending.", "new_tasks": [], "contradictions": []}'
        )
        result = critic_node(state)

    assert result["stop_flag"] is False


def test_critic_injects_new_tasks_on_continue(sub_queries):
    state = create_initial_state("test", max_iterations=6)
    state["redi_decomposition"] = sub_queries

    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking...",
            '{"action": "CONTINUE", "overall_confidence": 0.4, '
            '"reasoning": "Need more normative info.", '
            '"new_tasks": [{"sub_query_id": "sq_001", "assigned_agent": "NormativeAgent", '
            '"directive": "Check SNiP details", "priority": "high"}], '
            '"contradictions": []}'
        )
        result = critic_node(state)

    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["assigned_agent"] == "NormativeAgent"


def test_route_from_critic_returns_finish_on_stop_flag():
    state = create_initial_state("test")
    state["stop_flag"] = True
    assert route_from_critic(state) == "finish"


def test_route_from_critic_returns_continue_on_no_stop():
    state = create_initial_state("test")
    state["stop_flag"] = False
    state["critique"] = {"action": "CONTINUE"}
    assert route_from_critic(state) == "continue"


def test_critic_boundary_confidence_at_threshold(sub_queries):
    """Fact exactly at 0.7 threshold should be 'covered'."""
    state = create_initial_state("test", max_iterations=6)
    state["redi_decomposition"] = sub_queries
    state["output"] = [
        ConfirmedFact(
            fact_id="f1", claim="Claim at threshold", source_agents=["A"],
            confidence=0.7, citations=[], limitations=[], sub_query_id="sq_001"
        ),
        ConfirmedFact(
            fact_id="f2", claim="Recommended option at threshold", source_agents=["B"],
            confidence=0.7, citations=[], limitations=[], sub_query_id="sq_002",
            is_recommendation=True,
        ),
    ]
    result = critic_node(state)
    assert result["critique"]["action"] == "STOP"
    assert result["stop_flag"] is True


def test_critic_below_threshold_continues(sub_queries):
    """Fact at 0.69 (below 0.7) should be 'partial', not 'covered'."""
    state = create_initial_state("test", max_iterations=6)
    state["redi_decomposition"] = sub_queries
    state["output"] = [
        ConfirmedFact(
            fact_id="f1", claim="Claim below threshold", source_agents=["A"],
            confidence=0.69, citations=[], limitations=[], sub_query_id="sq_001"
        ),
        ConfirmedFact(
            fact_id="f2", claim="Another claim", source_agents=["B"],
            confidence=0.69, citations=[], limitations=[], sub_query_id="sq_002"
        ),
    ]
    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking...",
            '{"action": "CONTINUE", "overall_confidence": 0.55, '
            '"reasoning": "Confidence below threshold.", "new_tasks": [], "contradictions": []}'
        )
        result = critic_node(state)

    # Coverage should be "partial" not "covered"
    coverage = result["critique"]["coverage"]
    assert coverage.get("sq_001") == "partial"
