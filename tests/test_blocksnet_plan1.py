"""Tests for BlocksNet orchestration, gates, and hypothesis propagation."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from fp2mp_core.graph import _collect_agent_outputs
from fp2mp_core.nodes.agents.blocksnet import _parse_output
from fp2mp_core.nodes.agents.code_spatial import _SYSTEM_PROMPT as CODE_SPATIAL_PROMPT
from fp2mp_core.config import get_model_locality
from fp2mp_core.nodes.synthesis import measured_indicators_digest
from fp2mp_core.nodes.critic import critic_node
from fp2mp_core.nodes.orchestrator import _llm_choose_agent, _model_locality_matches, orchestrator_node
from fp2mp_core.summarize import compress_text
from fp2mp_core.state import RawEntry, Task, create_initial_state
from fp2mp_core.tools.blocksnet.data import make_data_tools
from fp2mp_core.tools.blocksnet.provision import _service_df, make_provision_tools


def _latin_name_to_cyrillic_for_test(name: str) -> str:
    replacements = (
        ("shch", "щ"),
        ("yo", "ё"),
        ("yu", "ю"),
        ("ya", "я"),
        ("zh", "ж"),
        ("kh", "х"),
        ("ts", "ц"),
        ("ch", "ч"),
        ("sh", "ш"),
    )
    single = {
        "a": "а",
        "b": "б",
        "c": "к",
        "d": "д",
        "e": "е",
        "f": "ф",
        "g": "г",
        "h": "х",
        "i": "и",
        "j": "й",
        "k": "к",
        "l": "л",
        "m": "м",
        "n": "н",
        "o": "о",
        "p": "п",
        "r": "р",
        "s": "с",
        "t": "т",
        "u": "у",
        "v": "в",
        "y": "ы",
        "z": "з",
    }
    value = name.lower()
    for latin, cyrillic in replacements:
        value = value.replace(latin, cyrillic)
    return "".join(single.get(char, char) for char in value).capitalize()


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


def test_blocksnet_confidence_counts_optimizer_and_scenario_verdict():
    content, confidence, limitations = _parse_output(
        "RESULT: proposed and tested scenario\n"
        "REFLECTION: block_id 10 improves after scenario provision test.\n"
        "HYPOTHESES: claim add school / test compute_scenario_provision / "
        "verdict: supported for block_id 10 strong 0.200->0.450\n"
        "LIMITATIONS: local model only",
        [
            {"tool": "propose_zone_development", "observation": "verdict: TPE-hypothesis for block_id [10] objective=0.2"},
            {"tool": "compute_scenario_provision", "observation": "verdict: supported for block_id [10]: school strong 0.200->0.450"},
        ],
    )

    assert confidence >= 0.7
    assert "unverified" not in content
    assert limitations == ["local model only"]


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

    assert confidence < 0.7
    assert content.startswith("HYPOTHESES:")
    assert "unverified" in content
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
    assert not any("compute_" in directive["question"] for directive in directives)
    assert any("дефициты" in directive["question"] for directive in directives)
    assert any("связна" in directive["question"] for directive in directives)
    assert any("городских центров" in directive["question"] for directive in directives)


def test_model_locality_matches_cyrillic_and_latin_queries_from_boundary():
    locality = get_model_locality()
    assert locality
    latin_name = str(locality["name"])
    cyrillic_name = _latin_name_to_cyrillic_for_test(latin_name)

    assert _model_locality_matches(f"Создай мастер-план города {cyrillic_name}")
    assert _model_locality_matches(f"Create a master plan for {latin_name}")
    assert not _model_locality_matches("Создай мастер-план города Мадрид")


def test_llm_choose_agent_prefers_blocksnet_for_matched_local_model_development():
    state = create_initial_state("Создай мастер-план города Гатчина")
    sq = {
        "sub_query_id": "sq_001",
        "text": "Предложи развитие сервисов и землепользования для города Гатчина",
        "search_modality": "code",
    }

    with patch("fp2mp_core.nodes.orchestrator.get_model_locality") as mock_locality:
        mock_locality.return_value = {"name": "Gatchina", "display_name": "Gatchina, District"}
        agent = _llm_choose_agent(sq, state, exclude_agents={"WebSearchAgent", "NormativeAgent"})

    assert agent == "BlocksNetAgent"


def test_code_spatial_prompt_does_not_require_output_dir_variable():
    assert "{{OUTPUT_DIR}}" in CODE_SPATIAL_PROMPT
    prompt_vars = set(re.findall(r"(?<!{){([A-Za-z_][A-Za-z0-9_]*)}(?!})", CODE_SPATIAL_PROMPT))
    assert prompt_vars == {"capability_cards"}


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
    assert "ANALYSIS PLAN" in result["tasks"][0]["directive"]


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


def test_service_df_returns_guidance_for_land_use_and_presets():
    state = {}

    land_use_result = _service_df(state, Path("data/model"), "RECREATION")
    preset_result = _service_df(state, Path("data/model"), "basic")

    assert isinstance(land_use_result, str)
    assert "похоже на тип землепользования" in land_use_result
    assert "Допустимые сервисы" in land_use_result
    assert isinstance(preset_result, str)
    assert "похоже на пресет" in preset_result
    assert "list_key_services" in preset_result


def test_measured_indicators_digest_preserves_tool_numbers_and_hypotheses():
    state = create_initial_state("Оцени сервисы")
    state["raw_data"] = [RawEntry(
        agent="BlocksNetAgent",
        sub_query_id="sq_001",
        type="blocksnet_result",
        content=(
            "ANALYSIS PLAN: check provision\n"
            "RESULT: school provision is 0.774 for block_id 10.\n"
            "REFLECTION: лучшие block_id 196 = 12.3 мин; худшие block_id 285 = 88.1 мин.\n"
            "HYPOTHESES: claim add school / test provision / verdict supported, block_id 10 = 0.774\n"
        ),
        confidence=0.78,
        tool_trace=[{
            "intermediate_steps": [
                {
                    "tool": "compute_service_provision",
                    "tool_input": "{'service_type': 'school'}",
                    "observation": "Суммарная сильная обеспеченность: 0.774\nПолная обеспеченность: 95 кварталов",
                },
                {
                    "tool": "list_key_services",
                    "tool_input": "{}",
                    "observation": "school accessibility=15",
                },
            ],
        }],
    )]

    digest = measured_indicators_digest(state)

    assert "0.774" in digest
    assert "95 кварталов" in digest
    assert "block_id 10" in digest
    assert "Ключевые кварталы (block_id)" in digest
    assert "block_id 196 = 12.3" in digest
    assert "block_id 285 = 88.1" in digest
    assert "verdict supported" in digest
    assert "list_key_services" not in digest


def test_measured_indicators_digest_surfaces_optimizer_artifacts():
    state = create_initial_state("Где строить сервисы")
    state["raw_data"] = [RawEntry(
        agent="BlocksNetAgent",
        sub_query_id="sq_001",
        type="blocksnet_result",
        content="RESULT: optimizer ran\nHYPOTHESES: verdict: TPE-hypothesis for block_id [10] objective=0.2",
        confidence=0.72,
        tool_trace=[{
            "intermediate_steps": [
                {
                    "tool": "propose_zone_development",
                    "tool_input": "{'block_ids': [10]}",
                    "observation": (
                        "HYPOTHESES: Оптимизатор предложил: school: 1 ед., capacity=120. "
                        "Распределение по кварталам: block_id 10: 1 ед. "
                        "Оценка обеспеченности: school: 0.200->0.450. "
                        "verdict: TPE-hypothesis for block_id [10] objective=0.2000."
                    ),
                },
            ],
        }],
    )]

    digest = measured_indicators_digest(state)

    assert "propose_zone_development" in digest
    assert "school: 1" in digest
    assert "0.200->0.450" in digest
    assert "block_id 10" in digest


def test_collect_agent_outputs_keeps_structured_blocksnet_content():
    raw_data = [RawEntry(
        entry_id="entry1",
        agent="BlocksNetAgent",
        sub_query_id="sq_001",
        iteration=1,
        type="blocksnet_result",
        content="ANALYSIS PLAN: plan\nRESULT: provision 0.774\nHYPOTHESES: verdict block_id 10",
        confidence=0.78,
    )]
    outputs = _collect_agent_outputs(raw_data, [{"source_entry_id": "entry1", "claim": "compressed claim"}])

    assert outputs[0]["agent"] == "BlocksNetAgent"
    assert outputs[0]["promotion"] == "promoted"
    assert "ANALYSIS PLAN" in outputs[0]["content"]
    assert "HYPOTHESES" in outputs[0]["content"]


def test_compute_service_provision_basic_runs_batch(tmp_path):
    blocks = pd.DataFrame(
        {
            "population": [100, 200, 0],
            "capacity_school": [120, 0, 0],
            "capacity_kindergarten": [0, 80, 0],
            "capacity_pharmacy": [1, 0, 0],
        }
    )
    provision_df = pd.DataFrame({"provision": [1.0, 0.4, 0.0]})
    state = {"acc_mx": pd.DataFrame([[0, 1, 2], [1, 0, 3], [2, 3, 0]])}
    tools = {
        tool.name: tool
        for tool in make_provision_tools({"state": state, "data_dir": Path("data/model"), "output_dir": tmp_path})
    }

    with patch("fp2mp_core.tools.blocksnet.provision.ensure_blocks", return_value=blocks), \
         patch("fp2mp_core.tools.blocksnet.provision.ensure_acc_mx", return_value=state["acc_mx"]), \
         patch("fp2mp_core.tools.blocksnet.provision.competitive_provision", return_value=provision_df), \
         patch("fp2mp_core.tools.blocksnet.provision.provision_strong_total", return_value=0.467), \
         patch("fp2mp_core.tools.blocksnet.provision.provision_weak_total", return_value=0.467), \
         patch("fp2mp_core.tools.blocksnet.provision.save_metric_map", return_value=None):
        result = tools["compute_service_provision"].invoke({"service_type": "basic"})

    assert "Батч-обеспеченность набора 'basic'" in result
    assert "| school |" in result
    assert "Ошибка: тип сервиса 'basic' не найден" not in result
    assert (tmp_path / "competitive_provision_batch_basic.csv").exists()
