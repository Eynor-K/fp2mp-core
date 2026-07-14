"""Behavioral ablation tests for ReDI, critic, and shared knowledge state."""

from unittest.mock import Mock

from fp2mp_core.ablation import AblationConfig
from fp2mp_core.nodes.blackboard import coverage_from_sub_queries, redi_decompose_node
from fp2mp_core.nodes.critic import critic_node
from fp2mp_core.nodes.curator import route_from_curator, wiki_curator_node


def test_redi_off_creates_one_unenriched_sub_query_without_llm(monkeypatch):
    monkeypatch.setattr(
        "fp2mp_core.nodes.blackboard.ReDIDecomposer", Mock(side_effect=AssertionError("LLM used"))
    )
    state = {"question": "whole question", "ablation": AblationConfig(redi=False)}

    result = redi_decompose_node(state)

    assert result["redi_decomposition"] == [{
        "sub_query_id": "sq_001",
        "text": "whole question",
        "intent_aspect": "",
        "search_modality": "any",
        "independence": True,
        "enriched_variants": [],
        "keywords": [],
        "domain_hints": [],
    }]


def test_critic_off_stops_when_no_pending_tasks_without_calling_llm(monkeypatch):
    monkeypatch.setattr(
        "fp2mp_core.nodes.critic.call_with_thinking", Mock(side_effect=AssertionError("LLM used"))
    )
    state = {
        "ablation": AblationConfig(critic=False),
        "iteration": 1,
        "max_iterations": 3,
        "stagnation_count": 0,
        "tasks": [{"status": "done"}],
        "redi_decomposition": [],
        "output": [],
    }

    result = critic_node(state)

    assert result["stop_flag"] is True
    assert result["critique"]["new_tasks"] == []


def test_blackboard_off_curator_returns_only_loop_control_and_raw_summary():
    state = {
        "ablation": AblationConfig(blackboard=False),
        "iteration": 0,
        "stagnation_count": 0,
        "raw_data": [{"type": "web_findings", "content": "evidence", "iteration": 0}],
        "wiki": {"index.md": {"content": "existing"}},
        "output": [{"claim": "existing"}],
    }

    result = wiki_curator_node(state)

    assert "wiki" not in result
    assert "output" not in result
    assert result["iteration"] == 1
    assert result["progress_delta"] == 1
    assert result["raw_data"][0]["type"] == "curator_summary"


def test_blackboard_off_coverage_uses_non_failed_raw_entries():
    state = {
        "ablation": AblationConfig(blackboard=False),
        "redi_decomposition": [{"sub_query_id": "sq_001"}, {"sub_query_id": "sq_002"}],
        "raw_data": [
            {"sub_query_id": "sq_001", "content": "evidence", "confidence": 0.8},
            {"sub_query_id": "sq_002", "content": "Search failed", "confidence": 0.1},
        ],
    }

    assert coverage_from_sub_queries(state) == {"sq_001": "covered", "sq_002": "pending"}
    assert route_from_curator({**state, "iteration": 1, "max_iterations": 2}) == "continue"
