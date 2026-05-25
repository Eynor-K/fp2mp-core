"""BlocksNetAgent — ReAct agent for local BlocksNet urban analytics."""

from __future__ import annotations

import re
from typing import Any

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool

from fp2mp_core.config import get_settings
from fp2mp_core.llm import get_chat_model
from fp2mp_core.runtime import get_run_dir
from fp2mp_core.state import BlackBoard, RawEntry, board_message
from fp2mp_core.tools.blocksnet import make_blocksnet_tools
from fp2mp_core.tools.blocksnet.registry import (
    format_blocksnet_cards,
    retrieve_blocksnet_capabilities,
)

_SYSTEM_PROMPT = """\
Ты — BlocksNetAgent, аналитик городской среды. Ты работаешь ТОЛЬКО с заранее
построенной моделью города в data/model: кварталы, ёмкости сервисов, население,
землепользование и предвычисленная матрица доступности.

Правила работы:
1. Данные подгружаются автоматически, но load_blocks(), load_accessibility_matrix(),
   list_cached_data() полезны для обзора модели и проверки кэша.
2. Не останавливайся после загрузки данных. Сразу вызывай нужные compute_* под задачу.
3. Допустимые типы сервисов бери из list_service_types(); нормативные demand/accessibility
   для ключевых сервисов бери из list_key_services(), а не угадывай.
4. PLAN должен ссылаться хотя бы на одну CARD id из Retrieved capability cards.
   Если ни одна карточка не подходит — вызови find_blocksnet_capability.
5. Все результаты автоматически сохраняются в data/model/output.
6. При ошибке инструмента объясни проблему и продолжай; не отказывайся от ответа.
7. Нельзя завершать ответ без финального блока RESULT/REFLECTION/HYPOTHESES/NUMERIC SELF-CHECK/CONFIDENCE/LIMITATIONS.
8. Если метрики указывают на открытые вопросы для проверки другими агентами, добавь FOLLOW_UPS.

Выбор метрики:
- обеспеченность/покрытие населения сервисом в N минут → compute_service_provision, не services_count;
- совместное покрытие сервисом → compute_shared_provision;
- доступность/связность → compute_mean_accessibility, compute_median_accessibility,
  compute_max_accessibility, compute_connectivity;
- доступность по землепользованию → compute_land_use_accessibility, compute_area_accessibility;
- плотность/количество/колокация/разнообразие сервисов → compute_services_density,
  compute_services_count, compute_services_collocation, compute_shannon_diversity;
- центральность → compute_services_centrality, build_adjacency_graph, compute_population_centrality;
- городская форма/освоенность → compute_density_indicators, compute_development_indicators.
- гипотезы развития/что и где построить/смена землепользования в зоне → сначала
  suggest_target_blocks, затем при явном запросе предложений propose_zone_development
  или optimize_zone_services.

Меню инструментов:
- data: load_blocks/load_accessibility_matrix для обзора; list_cached_data для кэша;
  list_service_types/list_key_services для service_type и нормативов; get_block_info/get_analysis_results для деталей.
- network: mean/median/max accessibility для времени доступности; connectivity для связности;
  land_use/area accessibility для зон и площадно-взвешенной доступности.
- provision: compute_service_provision для конкурентной обеспеченности; compute_shared_provision
  для совместной обеспеченности.
- services: density/count/collocation/Shannon diversity/centrality для сервисного каркаса.
- indicators: density/development indicators и adjacency_graph для морфологии и смежности.
- optimize: suggest_target_blocks для выбора зоны; propose_zone_development/optimize_zone_services для эвристической
  TPE-гипотезы набора сервисов и землепользования. Инструмент тяжёлый: вызывай только
  когда вопрос просит предложения или сценарий развития, а не для обычной диагностики.

Retrieved capability cards:
{capability_cards}

Финальный ответ должен быть ТОЛЬКО в таком формате:
RESULT: <ключевые числа модели с единицами и интерпретацией>
REFLECTION: <лучшие/худшие кварталы, дефициты обеспеченности, связь метрик; цитируй числа и block_id>
HYPOTHESES: <гипотезы развития из метрик: где добавить сервисы/зелень/уплотнение/смену функции; цитируй числа и block_id>
NUMERIC SELF-CHECK: <диапазоны/единицы/санити-чек по числам>
FOLLOW_UPS: <необязательные вопросы для следующих раундов; если агент очевиден, начни пункт с WebSearchAgent:, NormativeAgent:, CodeSpatialAgent: или BlocksNetAgent:>
CONFIDENCE: <0.0-1.0>
LIMITATIONS: <что не учтено; явно отметить, если выход за пределы data/model>

Не используй OSM, веб или произвольные внешние геоданные. Если вопрос выходит за
пределы локальной модели data/model, явно укажи это в LIMITATIONS.
"""
_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_FAILURE_MARKERS = ("Ошибка:", "Traceback", "Exception", "not found", "не найден")


def _build_agent(tools: list[BaseTool], capability_cards: str) -> AgentExecutor:
    llm = get_chat_model(temperature=0.0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        ("human", "Question: {input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ]).partial(capability_cards=capability_cards)
    agent = create_tool_calling_agent(
        llm=llm,
        tools=tools,
        prompt=prompt,
    )
    return AgentExecutor(
        agent=agent,
        tools=tools,
        max_iterations=10,
        return_intermediate_steps=True,
        verbose=False,
    )


def _parse_output(text: str, steps: list[dict[str, str]] | None = None) -> tuple[str, float, list[str]]:
    """Extract result, confidence, and limitations from agent output."""
    sections_map = _extract_sections(text)
    result = sections_map.get("RESULT", "")
    reflection = sections_map.get("REFLECTION", "")
    hypotheses = sections_map.get("HYPOTHESES", "")
    numeric_check = sections_map.get("NUMERIC SELF-CHECK", "")
    follow_ups = sections_map.get("FOLLOW_UPS", "")
    limitations: list[str] = []

    if sections_map.get("LIMITATIONS"):
        limitations = [lim.strip() for lim in sections_map["LIMITATIONS"].split(";") if lim.strip()]

    if not result:
        observation = _successful_numeric_observation(steps or [])
        if observation:
            result = f"Measured BlocksNet output: {observation}"
            limitations.append("Final answer did not expose RESULT; salvaged from successful tool output.")
        else:
            result = text[:500]

    if not reflection:
        reflection = _build_reflection(text, steps or [])

    compute_count = _successful_compute_count(steps or [])
    confidence = _confidence_from_steps(compute_count, bool(reflection))
    sections = []
    if reflection:
        sections.append(f"REFLECTION: {reflection}")
    if hypotheses:
        sections.append(f"HYPOTHESES: {hypotheses}")
    if follow_ups:
        sections.append(f"FOLLOW_UPS: {follow_ups}")
    sections.append(f"RESULT: {result}")
    if numeric_check:
        sections.append(f"NUMERIC SELF-CHECK: {numeric_check}")
    return "\n".join(sections), confidence, limitations


def _extract_sections(text: str) -> dict[str, str]:
    names = ("RESULT", "REFLECTION", "HYPOTHESES", "NUMERIC SELF-CHECK", "FOLLOW_UPS", "CONFIDENCE", "LIMITATIONS")
    pattern = re.compile(rf"^({'|'.join(re.escape(name) for name in names)}):\s*(.*)$", re.MULTILINE)
    matches = list(pattern.finditer(text or ""))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = (match.group(2) + text[start:end]).strip()
        sections[name] = body
    return sections


def _successful_numeric_observation(steps: list[dict[str, str]]) -> str:
    for step in reversed(steps):
        observation = step.get("observation", "").strip()
        if not observation or observation.startswith(_FAILURE_MARKERS):
            continue
        if _NUMBER_RE.search(observation):
            return observation[:1000]
    return ""


def _successful_compute_count(steps: list[dict[str, str]]) -> int:
    tools = set()
    for step in steps:
        tool = step.get("tool", "")
        observation = step.get("observation", "").strip()
        if tool.startswith("compute_") and observation and not observation.startswith(_FAILURE_MARKERS):
            tools.add(tool)
    return len(tools)


def _confidence_from_steps(compute_count: int, has_reflection: bool) -> float:
    if compute_count <= 0:
        return 0.3
    if compute_count <= 2:
        return 0.55
    return 0.78 if has_reflection else 0.68


def _build_reflection(text: str, steps: list[dict[str, str]]) -> str:
    compute_observations = [
        f"{step.get('tool')}: {step.get('observation')}"
        for step in steps
        if str(step.get("tool", "")).startswith("compute_")
        and step.get("observation")
        and not str(step.get("observation", "")).startswith(_FAILURE_MARKERS)
    ]
    if not compute_observations:
        return ""
    prompt = (
        "Сформулируй краткий блок REFLECTION для BlocksNet-анализа по наблюдениям compute_*. "
        "Нужно связать метрики, назвать лучшие/худшие кварталы или block_id, если они есть, "
        "и процитировать конкретные числа. Не добавляй внешние данные.\n\n"
        + "\n\n".join(compute_observations)[:6000]
        + f"\n\nЧерновой ответ агента:\n{text[:1000]}"
    )
    try:
        response = get_chat_model(temperature=0.0).invoke(prompt)
        reflection = response.content if hasattr(response, "content") else str(response)
        return reflection.strip().removeprefix("REFLECTION:").strip()[:1200]
    except Exception:
        return compute_observations[-1][:1000]


def _format_steps(steps: list[Any]) -> list[dict[str, str]]:
    formatted: list[dict[str, str]] = []
    for action, observation in steps:
        formatted.append(
            {
                "tool": getattr(action, "tool", ""),
                "tool_input": str(getattr(action, "tool_input", ""))[:500],
                "observation": str(observation)[:1000],
            }
        )
    return formatted


def blocksnet_agent_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for BlocksNetAgent."""
    directives = state.get("orchestrator_directives", [])
    iteration = state.get("iteration", 0)

    my_directives = [d for d in directives if d.get("target_agent") == "BlocksNetAgent"]
    if not my_directives:
        return {"raw_data": [], "agent_trace": [{"node": "blocksnet_agent", "skipped": True}]}

    new_entries: list[RawEntry] = []
    trace: list[dict[str, Any]] = []

    for directive in my_directives:
        question = directive.get("question", "")
        sq_id = directive.get("sub_query_id", "")
        settings = get_settings()
        tool_state: dict[str, Any] = {}
        output_dir = get_run_dir(settings.model_output_dir)
        tools = make_blocksnet_tools(tool_state, settings.model_data_dir, output_dir)
        cards = retrieve_blocksnet_capabilities(question, k=4)
        capability_context = format_blocksnet_cards(cards)
        executor = _build_agent(tools, capability_context)

        try:
            result = executor.invoke({"input": question})
            output_text = result.get("output", "")
            intermediate_steps = _format_steps(result.get("intermediate_steps", []))
            answer, confidence, limitations = _parse_output(output_text, intermediate_steps)
            entry = board_message(
                agent="BlocksNetAgent",
                iteration=iteration,
                msg_type="blocksnet_result",
                content=answer + (f"\nLimitations: {'; '.join(limitations)}" if limitations else ""),
                sub_query_id=sq_id,
                confidence=confidence,
                tool_trace=[{
                    "directive": directive,
                    "capability_cards": [card.get("id", "") for card in cards],
                    "raw_output": output_text[:400],
                    "intermediate_steps": intermediate_steps,
                }],
            )
            new_entries.append(entry)
            trace.append({"agent": "BlocksNetAgent", "sq_id": sq_id, "confidence": confidence})
        except Exception as exc:
            err_entry = board_message(
                agent="BlocksNetAgent",
                iteration=iteration,
                msg_type="blocksnet_result",
                content=(
                    "BlocksNetAgent low-confidence. Execution did not complete: "
                    f"{exc}. Treat this as missing augmentation, not as evidence "
                    "against answering."
                ),
                sub_query_id=sq_id,
                confidence=0.3,
            )
            new_entries.append(err_entry)
            trace.append({
                "agent": "BlocksNetAgent",
                "sq_id": sq_id,
                "confidence": 0.3,
                "partial": True,
            })

    return {
        "raw_data": new_entries,
        "agent_trace": trace,
    }
