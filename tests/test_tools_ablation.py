"""Agent behavior when the tools ablation is enabled."""

from unittest.mock import Mock

from fp2mp_core.ablation import AblationConfig
from fp2mp_core.nodes.agents import code_spatial, normative, web_search


def _state(agent: str):
    return {
        "ablation": AblationConfig(tools=False),
        "iteration": 1,
        "orchestrator_directives": [
            {"target_agent": agent, "question": "test question", "sub_query_id": "sq_001"}
        ],
    }


def test_web_agent_uses_single_llm_call_without_building_tools(monkeypatch):
    monkeypatch.setattr(
        web_search,
        "run_toolless",
        Mock(return_value="ANSWER: direct\nCONFIDENCE: 0.4\nSOURCES: "),
    )
    monkeypatch.setattr(web_search, "_build_agent", Mock(side_effect=AssertionError("tools used")))

    result = web_search.web_search_agent_node(_state("WebSearchAgent"))

    assert result["raw_data"][0]["content"] == "direct"
    assert result["raw_data"][0]["tool_trace"] == []


def test_normative_agent_uses_single_llm_call_without_building_tools(monkeypatch):
    monkeypatch.setattr(
        normative,
        "run_toolless",
        Mock(return_value="ANSWER: direct\nCONFIDENCE: 0.4\nSOURCES: "),
    )
    monkeypatch.setattr(normative, "_build_agent", Mock(side_effect=AssertionError("tools used")))

    result = normative.normative_agent_node(_state("NormativeAgent"))

    assert result["raw_data"][0]["content"] == "direct"
    assert result["raw_data"][0]["tool_trace"] == []


def test_code_agent_uses_single_llm_call_without_capability_lookup(monkeypatch):
    monkeypatch.setattr(
        code_spatial,
        "run_toolless",
        Mock(
            return_value=(
                "RESULT: direct\nNUMERIC SELF-CHECK: not applicable\n"
                "CONFIDENCE: 0.4\nLIMITATIONS: no tools"
            )
        ),
    )
    monkeypatch.setattr(
        code_spatial,
        "retrieve_capabilities",
        Mock(side_effect=AssertionError("capability lookup used")),
    )

    result = code_spatial.code_spatial_agent_node(_state("CodeSpatialAgent"))

    assert result["raw_data"][0]["content"].startswith("direct")
    assert result["raw_data"][0]["tool_trace"] == []
