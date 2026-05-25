"""BlocksNetAgent — ReAct agent for local BlocksNet urban analytics."""

from __future__ import annotations

from typing import Any

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool

from fp2mp_core.config import get_settings
from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard, RawEntry, board_message
from fp2mp_core.tools.blocksnet import make_blocksnet_tools

_SYSTEM_PROMPT = """\
Ты — BlocksNetAgent, аналитик городской среды. Ты работаешь ТОЛЬКО с заранее
построенной моделью города в data/model: кварталы, ёмкости сервисов, население,
землепользование и предвычисленная матрица доступности.

Правила работы:
1. Перед любым анализом вызови load_blocks() и load_accessibility_matrix().
2. Используй list_cached_data(), чтобы не загружать данные повторно.
3. Допустимые типы сервисов бери из list_service_types().
4. Все результаты автоматически сохраняются в data/model/output.
5. При ошибке инструмента объясни проблему и продолжай; не отказывайся от ответа.
6. Заверши строго блоком формата:
RESULT: <измеренные значения и их интерпретация>
CONFIDENCE: <0.0-1.0>
LIMITATIONS: <чего анализ не учитывает>

Не используй OSM, веб или произвольные внешние геоданные. Если вопрос выходит за
пределы локальной модели data/model, явно укажи это в LIMITATIONS.
"""


def _build_agent(tools: list[BaseTool]) -> AgentExecutor:
    llm = get_chat_model(temperature=0.0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        ("human", "Question: {input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
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


def _parse_output(text: str) -> tuple[str, float, list[str]]:
    """Extract result, confidence, and limitations from agent output."""
    result = ""
    confidence = 0.0
    limitations: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            result = line[len("RESULT:"):].strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line[len("CONFIDENCE:"):].strip())
            except ValueError:
                pass
        elif line.startswith("LIMITATIONS:"):
            limitations = [
                lim.strip()
                for lim in line[len("LIMITATIONS:"):].split(";")
                if lim.strip()
            ]

    if not result:
        result = text[:500]
        confidence = min(confidence or 0.3, 0.3)

    confidence = max(0.0, min(confidence, 1.0))
    return result, confidence, limitations


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
        tools = make_blocksnet_tools(tool_state, settings.model_data_dir, settings.model_output_dir)
        executor = _build_agent(tools)

        try:
            result = executor.invoke({"input": question})
            output_text = result.get("output", "")
            intermediate_steps = _format_steps(result.get("intermediate_steps", []))
            answer, confidence, limitations = _parse_output(output_text)
            entry = board_message(
                agent="BlocksNetAgent",
                iteration=iteration,
                msg_type="blocksnet_result",
                content=answer + (f"\nLimitations: {'; '.join(limitations)}" if limitations else ""),
                sub_query_id=sq_id,
                confidence=confidence,
                tool_trace=[{
                    "directive": directive,
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
