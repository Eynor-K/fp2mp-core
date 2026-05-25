"""Tests for BlocksNet orchestration, gates, and hypothesis propagation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fp2mp_core.nodes.agents.blocksnet import _parse_output
from fp2mp_core.nodes.critic import critic_node
from fp2mp_core.nodes.orchestrator import orchestrator_node
from fp2mp_core.summarize import compress_text
from fp2mp_core.state import RawEntry, Task, create_initial_state
from fp2mp_core.tools.blocksnet.data import make_data_tools


def test_list_key_services_reads_model_metadata():
    state = {}
    tools = {
        tool.name: tool
        for tool in make_data_tools({"state": state, "data_dir": Path("data/model")})
    }

    result = tools["list_key_services"].invoke({})

    assert "school" in result
    assert "accessibility=15" in result
    assert "demand=120" in result


def test_blocksnet_confidence_gate_uses_successful_compute_count():
    no_compute, no_compute_confidence, _ = _parse_output(
        "RESULT: loaded data only\nLIMITATIONS: no metrics",
        [{"tool": "load_blocks", "observation": "Кварталы загружены: 10"}],
    )
    assert no_compute_confidence <= 0.3
    assert "RESULT:" in no_compute

    rich_result, rich_confidence, _ = _parse_output(
        "RESULT: metrics computed\nREFLECTION: block_id 1 хуже, block_id 2 лучше\nLIMITATIONS: local model only",
        [
            {"tool": "compute_service_provision", "observation": "Полная обеспеченность: 10"},
            {"tool": "compute_mean_accessibility", "observation": "Мин: 1, макс: 20"},
            {"tool": "compute_shannon_diversity", "observation": "макс: 0.8"},
        ],
    )
    assert rich_confidence >= 0.7
    assert "REFLECTION:" in rich_result


def test_blocksnet_parse_output_is_interpretation_first_with_hypotheses():
    content, confidence, _ = _parse_output(
        "RESULT: mean accessibility 40 min; FSI 0.8\n"
        "REFLECTION: block_id 10 has weak service provision and 40 min access.\n"
        "HYPOTHESES: add local services near block_id 10 because provision is 0.2.\n"
        "FOLLOW_UPS: NormativeAgent: check service placement constraints for block_id 10.\n"
        "NUMERIC SELF-CHECK: values are minutes and ratios.\n"
        "LIMITATIONS: local model only",
        [
            {"tool": "compute_service_provision", "observation": "provision 0.2"},
            {"tool": "compute_mean_accessibility", "observation": "40 min"},
            {"tool": "compute_density_indicators", "observation": "FSI 0.8"},
        ],
    )

    assert confidence >= 0.7
    assert content.startswith("REFLECTION:")
    assert content.index("HYPOTHESES:") < content.index("RESULT:")
    assert "FOLLOW_UPS:" in content


def test_compress_text_falls_back_without_llm_for_long_text():
    text = "block_id 10 provision 0.2 " + ("x" * 1200) + " block_id 20 FSI 0.8"
    with patch("fp2mp_core.summarize.get_chat_model", side_effect=RuntimeError("no llm")):
        result = compress_text(text, max_chars=120, focus="preserve numbers and block_id")

    assert len(result) <= 120
    assert "block_id 10" in result
    assert "block_id 20" in result


def test_orchestrator_expands_blocksnet_model_locality_directive():
    state = create_initial_state("Оцени обеспеченность и доступность Гатчины")
    state["iteration"] = 0
    state["redi_decomposition"] = [{
        "sub_query_id": "sq_001",
        "text": "Оцени обеспеченность сервисами и доступность кварталов Гатчины",
        "search_modality": "code",
        "enriched_variants": [],
        "domain_hints": [],
    }]
    state["tasks"] = [Task(task_id="t1", sub_query_id="sq_001", status="pending")]
    state["raw_data"] = []

    with patch("fp2mp_core.nodes.orchestrator.get_model_locality") as mock_locality, \
         patch("fp2mp_core.nodes.orchestrator._llm_choose_agent", return_value="BlocksNetAgent"):
        mock_locality.return_value = {"name": "Гатчина", "display_name": "Гатчина, Ленинградская область"}
        result = orchestrator_node(state)

    directives = result["orchestrator_directives"]
    assert len(directives) == 3
    assert all(directive["target_agent"] == "BlocksNetAgent" for directive in directives)
    assert any("compute_service_provision" in directive["question"] for directive in directives)
    assert any("compute_connectivity" in directive["question"] for directive in directives)
    assert any("compute_density_indicators" in directive["question"] for directive in directives)


def test_critic_requests_blocksnet_followup_for_thin_result():
    state = create_initial_state("Оцени обеспеченность Гатчины")
    state["iteration"] = 1
    state["max_iterations"] = 6
    state["stagnation_count"] = 0
    state["redi_decomposition"] = [{
        "sub_query_id": "sq_001",
        "text": "Оцени обеспеченность сервисами кварталов Гатчины",
    }]
    state["tasks"] = []
    state["raw_data"] = [RawEntry(
        agent="BlocksNetAgent",
        sub_query_id="sq_001",
        type="blocksnet_result",
        content="RESULT: Кварталы загружены",
        confidence=0.3,
        tool_trace=[{"intermediate_steps": [{"tool": "load_blocks", "observation": "ok"}]}],
    )]

    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking",
            '{"action": "STOP", "overall_confidence": 0.8, "reasoning": "enough", '
            '"new_tasks": [], "contradictions": []}',
        )
        result = critic_node(state)

    assert result["stop_flag"] is False
    assert result["tasks"]
    assert result["tasks"][0]["assigned_agent"] == "BlocksNetAgent"
    assert "compute_*" in result["tasks"][0]["directive"]


def test_critic_can_promote_explicit_blocksnet_followups_on_continue():
    state = create_initial_state("Проверь гипотезы развития")
    state["iteration"] = 1
    state["max_iterations"] = 6
    state["stagnation_count"] = 0
    state["redi_decomposition"] = [{"sub_query_id": "sq_001", "text": "Гипотезы развития"}]
    state["tasks"] = []
    state["raw_data"] = [RawEntry(
        agent="BlocksNetAgent",
        sub_query_id="sq_001",
        type="blocksnet_result",
        content=(
            "REFLECTION: block_id 10 has deficit.\n"
            "HYPOTHESES: add school near block_id 10.\n"
            "FOLLOW_UPS: NormativeAgent: check whether school placement near block_id 10 is permitted.\n"
            "RESULT: provision 0.2\n"
        ),
        confidence=0.78,
        tool_trace=[{"intermediate_steps": [{"tool": "compute_service_provision", "observation": "0.2"}]}],
    )]

    with patch("fp2mp_core.nodes.critic.call_with_thinking") as mock_thinking:
        mock_thinking.return_value = (
            "thinking",
            '{"action": "CONTINUE", "overall_confidence": 0.6, "reasoning": "validate", '
            '"new_tasks": [], "contradictions": []}',
        )
        result = critic_node(state)

    assert result["stop_flag"] is False
    assert result["tasks"][0]["assigned_agent"] == "NormativeAgent"
    assert "block_id 10" in result["tasks"][0]["directive"]
