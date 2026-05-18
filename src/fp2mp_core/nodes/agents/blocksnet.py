"""
BlocksNetAgent node — встроенный агент городского анализа.

Использует библиотеку blocksnet (23 инструмента) для анализа транспортной
доступности, обеспеченности сервисами, плотности застройки и централности
городских кварталов.

Данные берутся из data/ директории fp2mp_core (blocks_with_services.gpkg,
acc_mx.pickle, platform/*.geojson и др.).

Синглтон с double-checked locking: геоданные загружаются один раз
и переиспользуются между итерациями LangGraph.
"""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from fp2mp_core.config import DATA_DIR
from fp2mp_core.failure import assess_confidence
from fp2mp_core.llm import get_chat_model
from fp2mp_core.nodes.context import parse_follow_ups
from fp2mp_core.state import BlackBoard, RawEntry, board_message
from fp2mp_core.tools.blocksnet import make_tools
from fp2mp_core.tools.blocksnet.prompts import build_system_prompt

_OUTPUT_DIR = DATA_DIR / "outputs"

_lock = threading.Lock()
_runner: _BlocksNetRunner | None = None  # type: ignore[name-defined]


class _BlocksNetRunner:
    """Обёртка над LangGraph ReAct-агентом с кэшем геоданных."""

    def __init__(self) -> None:
        self._state: dict = {}
        _OUTPUT_DIR.mkdir(exist_ok=True)
        llm = get_chat_model(temperature=0, max_tokens=4096)
        tools = make_tools(self._state, DATA_DIR, _OUTPUT_DIR)
        self._graph = create_react_agent(model=llm, tools=tools, prompt=build_system_prompt(tools))

    def run(self, task: str) -> dict[str, Any]:
        try:
            result = self._graph.invoke({"messages": [HumanMessage(content=task)]})
        except Exception as exc:
            return {"input": task, "output": f"Ошибка при запуске агента: {exc}", "messages": []}

        messages = result.get("messages", [])
        output = next(
            (str(m.content) for m in reversed(messages) if isinstance(m, AIMessage) and m.content),
            "Ответ не получен.",
        )
        return {"input": task, "output": output, "messages": messages}

    def reset(self) -> None:
        self._state.clear()


def _get_runner() -> _BlocksNetRunner:
    global _runner
    if _runner is not None:
        return _runner
    with _lock:
        if _runner is not None:
            return _runner
        _runner = _BlocksNetRunner()
    return _runner


def _extract_tool_trace(messages: list) -> list[dict[str, Any]]:
    """Конвертирует лог сообщений blocksnet-агента в формат tool_trace fp2mp_core."""
    steps: list[dict[str, Any]] = []
    raw_output = ""

    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in (getattr(msg, "tool_calls", None) or []):
                steps.append({
                    "tool": tc.get("name", ""),
                    "tool_input": str(tc.get("args", ""))[:500],
                    "observation": "",
                })
            if not getattr(msg, "tool_calls", None) and msg.content:
                raw_output = str(msg.content)[:600]
        elif isinstance(msg, ToolMessage):
            tool_name = getattr(msg, "name", "")
            for step in reversed(steps):
                if step["observation"] == "" and step["tool"] == tool_name:
                    step["observation"] = str(msg.content)[:800]
                    break

    return [{"directive": {}, "raw_output": raw_output, "intermediate_steps": steps}]


def blocksnet_agent_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node — BlocksNetAgent для анализа городских данных."""
    directives = [
        d for d in state.get("orchestrator_directives", [])
        if d.get("target_agent") == "BlocksNetAgent"
    ]
    if not directives:
        return {"raw_data": [], "agent_trace": [{"node": "blocksnet_agent", "skipped": True}]}

    runner = _get_runner()
    iteration = state.get("iteration", 0)
    new_entries: list[RawEntry] = []
    trace: list[dict[str, Any]] = []

    for directive in directives:
        question = directive.get("question", "")
        sq_id = directive.get("sub_query_id", "")

        try:
            result = runner.run(question)
            output_text: str = result.get("output", "")

            tool_trace = _extract_tool_trace(result.get("messages", []))
            if tool_trace:
                tool_trace[0]["directive"] = directive

            confidence, failed = assess_confidence(output_text, tool_trace, parsed_confidence=0.75)
            content = output_text
            follow_ups: list[dict[str, str]] = []
            if failed:
                content = (
                    "[FAILED] BlocksNetAgent did not produce a reliable result. "
                    f"Raw output: {output_text[:500]}"
                )
            else:
                follow_ups = parse_follow_ups(output_text)

            new_entries.append(board_message(
                agent="BlocksNetAgent",
                iteration=iteration,
                msg_type="urban_analysis",
                content=content,
                sub_query_id=sq_id,
                confidence=confidence,
                tool_trace=tool_trace,
                follow_up_suggestions=follow_ups,
            ))
            trace.append({"agent": "BlocksNetAgent", "sq_id": sq_id, "confidence": confidence})

        except Exception as exc:
            new_entries.append(board_message(
                agent="BlocksNetAgent",
                iteration=iteration,
                msg_type="urban_analysis",
                content=f"BlocksNetAgent failed: {exc}",
                sub_query_id=sq_id,
                confidence=0.1,
            ))

    return {"raw_data": new_entries, "agent_trace": trace}
