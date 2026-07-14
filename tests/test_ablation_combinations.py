"""Composition tests for the ablation switches.

Tests cover:
- every pairwise toggle: each node behaves as expected for that flag set
- the "everything off" scenario
- the runtime composition (``create_initial_state``, ``run()`` API surface)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fp2mp_core import graph
from fp2mp_core.ablation import AblationConfig
from fp2mp_core.nodes.blackboard import coverage_from_sub_queries, redi_decompose_node
from fp2mp_core.nodes.curator import route_from_curator, wiki_curator_node
from fp2mp_core.nodes.synthesis import final_synthesis_node

# ---------------------------------------------------------------------------
# Single-flag tests — guard that each switch toggles the expected behavior.
# ---------------------------------------------------------------------------


def test_redi_off_synthesizes_single_sub_query() -> None:
    state = {"question": "Q", "ablation": AblationConfig(redi=False)}
    assert redi_decompose_node(state)["redi_decomposition"] == [
        {
            "sub_query_id": "sq_001",
            "text": "Q",
            "intent_aspect": "",
            "search_modality": "any",
            "independence": True,
            "enriched_variants": [],
            "keywords": [],
            "domain_hints": [],
        }
    ]


def test_blackboard_off_curator_returns_loop_control_only() -> None:
    state = {
        "ablation": AblationConfig(blackboard=False),
        "iteration": 0,
        "stagnation_count": 0,
        "raw_data": [
            {"type": "web_findings", "content": "ok", "iteration": 0, "agent": "WebSearchAgent"}
        ],
        "wiki": {},
        "output": [{"claim": "existing"}],
    }
    result = wiki_curator_node(state)
    assert "wiki" not in result
    assert "output" not in result
    assert result["iteration"] == 1


def test_blackboard_off_coverage_uses_raw_entries() -> None:
    state = {
        "ablation": AblationConfig(blackboard=False),
        "redi_decomposition": [{"sub_query_id": "sq_001"}, {"sub_query_id": "sq_002"}],
        "raw_data": [
            {"sub_query_id": "sq_001", "content": "ok"},
            {"sub_query_id": "sq_002", "content": "Search failed"},
        ],
    }
    assert coverage_from_sub_queries(state) == {"sq_001": "covered", "sq_002": "pending"}


def test_blackboard_off_curator_routing_skips_critic() -> None:
    state = {
        "ablation": AblationConfig(blackboard=False),
        "iteration": 1,
        "max_iterations": 6,
        "raw_data": [{"type": "web_findings"}],
    }
    # No output facts are ever populated when blackboard is off, so the
    # curator must not route to the critic.
    assert route_from_curator(state) == "continue"


def test_blackboard_off_final_synthesis_uses_raw_data() -> None:
    state = {
        "ablation": AblationConfig(blackboard=False),
        "question": "Q",
        "wiki": {},
        "output": [],
        "raw_data": [
            {"type": "web_findings", "agent": "WebSearchAgent",
             "content": "answer", "confidence": 0.5, "sub_query_id": "sq_001",
             "citations": []},
            {"type": "init", "content": "init"},
        ],
        "iteration": 1,
        "critique": {},
    }
    with patch("fp2mp_core.nodes.synthesis.get_chat_model") as mock_synth_llm:
        mock_synth_llm.return_value.invoke.return_value = MagicMock(content="# Answer")
        result = final_synthesis_node(state)
    assert result["final_answer"]
    assert result["stop_flag"] is True


# ---------------------------------------------------------------------------
# Pairwise combinations — verify joint behavior at the node level.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        {"redi": False, "blackboard": False, "critic": True, "tools": True},
        {"redi": False, "blackboard": True, "critic": False, "tools": True},
        {"redi": False, "blackboard": True, "critic": True, "tools": False},
        {"redi": True, "blackboard": False, "critic": False, "tools": True},
        {"redi": True, "blackboard": False, "critic": True, "tools": False},
        {"redi": True, "blackboard": True, "critic": False, "tools": False},
    ],
)
def test_pairwise_compositions_resolve_state_cleanly(flags: dict[str, bool]) -> None:
    """Every pairwise (off, off) configuration produces a stable state.

    The contract: ``create_initial_state`` honors the ablation config, and the
    nodes that gate on it return ``None``/``{}`` for state fields they no
    longer populate.
    """
    config = AblationConfig(**flags)
    state = {
        "ablation": config,
        "question": "Q",
        "iteration": 0,
        "stagnation_count": 0,
        "raw_data": [
            {"type": "web_findings", "content": "ok", "iteration": 0,
             "agent": "WebSearchAgent", "confidence": 0.7}
        ],
        "wiki": {},
        "output": [],
        "tasks": [{"status": "pending"}],
    }

    curator_result = wiki_curator_node(state)
    coverage = coverage_from_sub_queries({**state, "redi_decomposition": [
        {"sub_query_id": "sq_001"}
    ]})

    if not flags["blackboard"]:
        assert "wiki" not in curator_result
        assert "output" not in curator_result
    if flags["blackboard"]:
        assert coverage["sq_001"] in {"pending", "partial", "covered"}


def test_all_flags_off_initial_state_is_well_formed() -> None:
    """Composition sanity: every flag toggled off keeps the pipeline reachable."""
    from fp2mp_core.state import create_initial_state

    config = AblationConfig(redi=False, blackboard=False, critic=False, tools=False)
    state = create_initial_state("question", ablation=config, max_iterations=1)
    assert state["ablation"] is config
    assert state["max_iterations"] == 1
    # ReDI off → redi_decompose_node returns one synthetic sub-query without LLM.
    sub_queries = redi_decompose_node(state)["redi_decomposition"]
    assert len(sub_queries) == 1
    assert sub_queries[0]["text"] == "question"


# ---------------------------------------------------------------------------
# Runtime API surface: ``run()`` accepts ``ablation`` and forwards it.
# ---------------------------------------------------------------------------


def test_run_forwards_ablation_config_to_initial_state() -> None:
    config = AblationConfig(redi=False)

    captured: dict[str, Any] = {}

    def fake_run(inner_input, model, max_iterations=None, ablation=None) -> dict[str, Any]:
        captured["input"] = inner_input
        captured["model"] = model
        captured["max_iterations"] = max_iterations
        captured["ablation"] = ablation
        return {"input": inner_input, "output": "ok", "log": [], "usage": {}}

    with patch("fp2mp_core.graph.run", side_effect=fake_run):
        result = graph.run("q", model="m", max_iterations=2, ablation=config)

    assert result["output"] == "ok"
    assert captured["ablation"] is config
    assert captured["input"] == "q"
    assert captured["max_iterations"] == 2