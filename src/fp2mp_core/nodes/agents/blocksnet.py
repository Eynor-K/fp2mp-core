"""BlocksNetAgent — ReAct agent for local BlocksNet urban analytics."""

from __future__ import annotations

import re
from typing import Any

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool

from fp2mp_core.config import get_settings
from fp2mp_core.llm import get_chat_model
from fp2mp_core.nodes.blackboard import wiki_briefing
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
   compute_service_provision принимает конкретный сервис ИЛИ key/basic/advanced/comfort
   (тогда считает батч по набору сервисов).
4. PLAN должен ссылаться хотя бы на одну CARD id из Retrieved capability cards.
   Если ни одна карточка не подходит — вызови find_blocksnet_capability.
5. Все результаты автоматически сохраняются в data/model/output.
6. При ошибке инструмента объясни проблему и продолжай; не отказывайся от ответа.
7. Нельзя завершать ответ без финального блока ANALYSIS PLAN/RESULT/REFLECTION/HYPOTHESES/NUMERIC SELF-CHECK/CONFIDENCE/LIMITATIONS.
8. Если метрики указывают на открытые вопросы для проверки другими агентами, добавь FOLLOW_UPS.

Инвентарь инструментов: data для обзора модели и деталей block_id; network для доступности и связности;
provision для обеспеченности сервисами; services для плотности, разнообразия, колокации и центральности;
indicators для морфологии/освоенности; optimize для явных сценариев развития и подбора зоны.

ANALYSIS PLAN обязателен до расчётов: для ЭТОЙ задачи и контекста сформулируй
вопрос → информационные потребности → выбранные метрики/инструменты с обоснованием.
Не вызывай compute_* до того, как объяснил, зачем эта метрика нужна для задачи.
Если задача широкая, используй минимум 3 метрики разных семейств, но не делай лишних расчётов.

Проверка гипотез: сформулировал гипотезу → выбери метрику/детализацию, которая её проверяет
→ вызови compute_*, get_analysis_results(result_key) или get_block_info(block_id) → запиши verdict
с конкретным числом и block_id. Непроверенные гипотезы помечай unverified и не выдавай как факт.

Retrieved capability cards:
{capability_cards}

Финальный ответ должен быть ТОЛЬКО в таком формате:
ANALYSIS PLAN: <для этой задачи/контекста: какие сведения нужны и какие метрики их дают, с обоснованием выбора каждой>
RESULT: <ключевые числа модели с единицами и интерпретацией>
REFLECTION: <лучшие/худшие кварталы, дефициты обеспеченности, связь метрик; цитируй числа и block_id>
HYPOTHESES: <список: claim / test / verdict. Verdict обязан опираться на фактический вывод инструмента, число и block_id; иначе unverified>
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
        ("human", "Вопрос: {input}\n\nКонтекст (что уже известно от других агентов и какие пробелы):\n{context}"),
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
    analysis_plan = sections_map.get("ANALYSIS PLAN", "")
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

    unverified = False
    if hypotheses:
        hypotheses, unverified = _ensure_hypothesis_verdicts(hypotheses, steps or [])
        if unverified:
            limitations.append("Some BlocksNet hypotheses were not verified by tool output and are marked unverified.")

    compute_count = _successful_compute_count(steps or [])
    confidence = _confidence_from_steps(compute_count, bool(reflection))
    if unverified:
        confidence = min(confidence, 0.62)
    sections = []
    if analysis_plan:
        sections.append(f"ANALYSIS PLAN: {analysis_plan}")
    if hypotheses:
        sections.append(f"HYPOTHESES: {hypotheses}")
    if reflection:
        sections.append(f"REFLECTION: {reflection}")
    if follow_ups:
        sections.append(f"FOLLOW_UPS: {follow_ups}")
    sections.append(f"RESULT: {result}")
    if numeric_check:
        sections.append(f"NUMERIC SELF-CHECK: {numeric_check}")
    return "\n".join(sections), confidence, limitations


def _extract_sections(text: str) -> dict[str, str]:
    names = ("ANALYSIS PLAN", "RESULT", "REFLECTION", "HYPOTHESES", "NUMERIC SELF-CHECK", "FOLLOW_UPS", "CONFIDENCE", "LIMITATIONS")
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


def _ensure_hypothesis_verdicts(hypotheses: str, steps: list[dict[str, str]]) -> tuple[str, bool]:
    """Ensure hypotheses have verdicts grounded in tool output; mark unsafe ones unverified."""
    if _has_verdict_with_number(hypotheses):
        return hypotheses, False
    verified = _build_hypothesis_verdicts(hypotheses, steps)
    if verified and _has_verdict_with_number(verified):
        return verified, False
    suffix = "\n- verdict: unverified — нет достаточного фактического вывода инструмента с числом/block_id для проверки."
    return hypotheses.rstrip() + suffix, True


def _has_verdict_with_number(text: str) -> bool:
    for line in (text or "").splitlines():
        lowered = line.lower()
        if "verdict" in lowered or "вердикт" in lowered or "подтверж" in lowered or "опроверг" in lowered:
            if _NUMBER_RE.search(line):
                return True
    return False


def _build_hypothesis_verdicts(hypotheses: str, steps: list[dict[str, str]]) -> str:
    observations = [
        f"{step.get('tool')}({step.get('tool_input')}): {step.get('observation')}"
        for step in steps
        if step.get("observation") and not str(step.get("observation", "")).startswith(_FAILURE_MARKERS)
    ]
    if not observations:
        return ""
    prompt = (
        "Проверь гипотезы BlocksNet только по наблюдениям инструментов ниже. "
        "Для каждой гипотезы верни claim / test / verdict. Verdict должен содержать конкретное число "
        "или block_id из наблюдений; если проверить нечем, verdict: unverified. Не добавляй внешние данные.\n\n"
        f"HYPOTHESES:\n{hypotheses[:2000]}\n\nOBSERVATIONS:\n" + "\n\n".join(observations)[:6000]
    )
    try:
        response = get_chat_model(temperature=0.0).invoke(prompt)
        return (response.content if hasattr(response, "content") else str(response)).strip()[:2000]
    except Exception:
        return ""


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


def _selected_metrics(steps: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"tool": step.get("tool", ""), "tool_input": step.get("tool_input", "")}
        for step in steps
        if str(step.get("tool", "")).startswith("compute_")
    ]


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
        directive_question = directive.get("question", "")
        original_question = state.get("question", "")
        question = original_question or directive_question
        context = (
            f"Директива оркестратора: {directive_question}\n\n"
            f"Краткий бриф wiki/blackboard:\n{wiki_briefing(state, limit=1500)}"
        )
        sq_id = directive.get("sub_query_id", "")
        settings = get_settings()
        tool_state: dict[str, Any] = {}
        output_dir = get_run_dir(settings.model_output_dir)
        tools = make_blocksnet_tools(tool_state, settings.model_data_dir, output_dir)
        cards = retrieve_blocksnet_capabilities(f"{question}\n{directive_question}\n{context}", k=4)
        capability_context = format_blocksnet_cards(cards)
        executor = _build_agent(tools, capability_context)

        try:
            result = executor.invoke({"input": question, "context": context})
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
                    "context": context[:1500],
                    "analysis_plan": _extract_sections(output_text).get("ANALYSIS PLAN", "")[:1500],
                    "selected_metrics": _selected_metrics(intermediate_steps),
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
