"""Integration tests for the LangGraph graph assembly."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fp2mp_core.state import create_initial_state


def test_graph_compiles():
    """Smoke test: graph compiles without errors."""
    with patch("fp2mp_core.nodes.blackboard.ReDIDecomposer"), \
         patch("fp2mp_core.nodes.blackboard.ReDIEnricher"):
        from fp2mp_core.graph import build_graph
        graph = build_graph()
        assert graph is not None


def test_graph_has_expected_nodes():
    """Check that all required nodes are present in the compiled graph."""
    with patch("fp2mp_core.nodes.blackboard.ReDIDecomposer"), \
         patch("fp2mp_core.nodes.blackboard.ReDIEnricher"):
        from fp2mp_core.graph import build_graph
        graph = build_graph()
        node_names = set(graph.nodes)
        expected = {
            "redi_decompose", "init_blackboard", "orchestrator",
            "web_search_agent", "normative_agent", "code_spatial_agent",
            "mediator", "wiki_curator", "critic", "final_synthesis"
        }
        assert expected.issubset(node_names)


def test_full_loop_terminates_at_max_iterations(sub_queries):
    """Mock all LLM/tool calls and verify loop terminates at max_iterations."""
    from fp2mp_core.state import SubQuery

    mock_sub_queries = [
        SubQuery(sub_query_id="sq_001", text="Test question?",
                 intent_aspect="empirical", search_modality="web",
                 independence=True, enriched_variants=["variant"],
                 keywords=["keyword"], domain_hints=[]),
    ]

    mock_decomposer = MagicMock()
    mock_decomposer.return_value = mock_sub_queries
    mock_enricher = MagicMock()
    mock_enricher.enrich_all.return_value = mock_sub_queries

    mock_agent_result = {
        "output": "ANSWER: Some answer\nCONFIDENCE: 0.6\nSOURCES: https://example.com"
    }

    with patch("fp2mp_core.nodes.blackboard.ReDIDecomposer", return_value=mock_decomposer), \
         patch("fp2mp_core.nodes.blackboard.ReDIEnricher", return_value=mock_enricher), \
         patch("fp2mp_core.nodes.agents.web_search._build_agent") as mock_web_agent, \
         patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:

        mock_executor = MagicMock()
        mock_executor.invoke.return_value = mock_agent_result
        mock_web_agent.return_value = mock_executor

        mock_thinking.return_value = (
            "thinking...",
            '{"action": "CONTINUE", "overall_confidence": 0.4, '
            '"reasoning": "Need more info.", "new_tasks": [], "contradictions": []}'
        )

        from fp2mp_core.graph import build_graph
        graph = build_graph()

        initial = create_initial_state("Test question for loop termination?", max_iterations=2)
        result = graph.invoke(initial)

        assert result["stop_flag"] is True
        assert result["iteration"] <= 2 + 1  # curator increments once more after max


def test_final_answer_is_not_none_after_run(sub_queries):
    """After graph completes, final_answer must be set."""
    from fp2mp_core.state import SubQuery, ConfirmedFact

    mock_sub_queries = [
        SubQuery(sub_query_id="sq_001", text="Test?",
                 intent_aspect="empirical", search_modality="web",
                 independence=True, enriched_variants=[],
                 keywords=[], domain_hints=[]),
    ]

    mock_decomposer = MagicMock(return_value=mock_sub_queries)
    mock_enricher = MagicMock()
    mock_enricher.enrich_all.return_value = mock_sub_queries

    mock_agent_result = {
        "output": "ANSWER: Complete answer here with sufficient detail.\nCONFIDENCE: 0.8\nSOURCES: "
    }

    with patch("fp2mp_core.nodes.blackboard.ReDIDecomposer", return_value=mock_decomposer), \
         patch("fp2mp_core.nodes.blackboard.ReDIEnricher", return_value=mock_enricher), \
         patch("fp2mp_core.nodes.agents.web_search._build_agent") as mock_web_agent, \
         patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking, \
         patch("fp2mp_core.nodes.synthesis.get_chat_model") as mock_llm_factory:

        mock_executor = MagicMock()
        mock_executor.invoke.return_value = mock_agent_result
        mock_web_agent.return_value = mock_executor

        # Critic says STOP after first round
        mock_thinking.return_value = (
            "thinking...",
            '{"action": "STOP", "overall_confidence": 0.8, '
            '"reasoning": "Sufficient evidence.", "new_tasks": [], "contradictions": []}'
        )

        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "# Final Answer\n\nThis is the synthesized answer."
        mock_llm.invoke.return_value = mock_response
        mock_llm_factory.return_value = mock_llm

        from fp2mp_core.graph import build_graph
        graph = build_graph()

        initial = create_initial_state("What are the height limits near airports?", max_iterations=3)
        result = graph.invoke(initial)

        assert result.get("final_answer") is not None
        assert len(result["final_answer"]) > 10
