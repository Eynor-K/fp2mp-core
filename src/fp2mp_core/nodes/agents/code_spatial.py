"""
CodeSpatialAgent — ReAct agent for quantitative and spatial analysis.

Enforces mandatory chain-of-thought:
HYPOTHESIS -> LIBRARIES CHECK -> PLAN -> CODE -> EXECUTE -> INTERPRET.
Can fetch data from OpenStreetMap via osmnx, public APIs via requests/httpx,
and geocode addresses via geopy — no local data required.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool, StructuredTool

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard, RawEntry, board_message
from fp2mp_core.tools.code_exec import (
    check_available_data_tool,
    inspect_api_tool,
    list_available_libraries_tool,
    run_validated_python,
)
from fp2mp_core.tools.tool_registry import (
    find_capability_tool,
    format_capability_cards,
    retrieve_capabilities,
)

_SYSTEM_PROMPT = """\
You are the CodeSpatialAgent. You solve quantitative and spatial sub-queries by writing
and executing validated Python code. Tools augment the final answer; failures are
not a reason to refuse.

Use the injected capability cards as your source of API truth. Your PLAN must cite
at least one CARD id before writing code. If no card fits, call find_capability_tool.
Before using an uncertain function, call inspect_api_tool("module.function").

Workflow:
1. Call list_available_libraries_tool.
2. Optionally call check_available_data_tool or find_capability_tool.
3. PLAN with CARD id(s), data source, code outline, expected outputs, and fallback.
4. Execute raw Python with run_validated_python. Print every value needed.
5. If validation/runtime fails or output is empty, REVISE and retry with changed code.
6. Do not submit identical code twice. Change approach or finish with fallback.
7. INTERPRET the numbers. Include units, provenance/source_url when available,
   and a numeric self-check.

Location guardrail:
- If the task names a place, use only that explicit place as spatial scope.
- If the task does not name a place, do not substitute placeholders such as
  "region X", "<place>", or examples from cards. Either infer a real location
  from the task text or finish with LIMITATIONS: location not specified.
- NUMERIC SELF-CHECK is mandatory and must state what place/scope and units the
  number belongs to. CONFIDENCE is mandatory.

Prefer robust keyless sources: OSM/osmnx, Wikidata SPARQL, Open-Meteo, local DATA_DIR.
For osmnx use_cache and requests_timeout should be set in code.

Retrieved capability cards:
{capability_cards}

Finish only when RESULT contains concrete measured values or when you have an explicit
low-confidence fallback after failed retries. Use this final structure exactly:
HYPOTHESIS: <your expectation>
PLAN: <CARD ids used + pseudocode describing the approach>
RESULT: <measured values and interpretation of execution output>
NUMERIC SELF-CHECK: <units, ranges, and sanity checks for key numbers>
CONFIDENCE: <0.0-1.0>
LIMITATIONS: <what this analysis cannot account for>

Do not write artificial Observation text. Tool outputs arrive automatically.
"""

_BASE_TOOLS = [
    inspect_api_tool,
    find_capability_tool,
    check_available_data_tool,
    list_available_libraries_tool,
]
_SUCCESS_FAILURE_MARKERS = (
    "Validation failed:",
    "Execution timed out",
    "Subprocess error:",
    "E2B error:",
    "Traceback",
    "[stderr]",
)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _make_guarded_python_tool() -> BaseTool:
    seen_hashes: set[str] = set()
    failed_or_duplicate_count = 0

    def guarded_run_validated_python(code: str) -> str:
        """Validate and execute Python code; rejects identical repeated snippets."""
        nonlocal failed_or_duplicate_count
        normalized = "\n".join(line.rstrip() for line in code.strip().splitlines())
        code_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if code_hash in seen_hashes:
            failed_or_duplicate_count += 1
            return (
                "Validation failed: identical code already executed in this task. "
                "Change the code or data source, or finish with an explicit fallback."
            )
        seen_hashes.add(code_hash)

        result = run_validated_python.invoke({"code": code})
        if _is_successful_observation(str(result)):
            failed_or_duplicate_count = 0
        else:
            failed_or_duplicate_count += 1

        if failed_or_duplicate_count >= 3:
            return (
                f"{result}\n\nLoop guard: three consecutive failed or duplicate executions. "
                "Stop executing code and produce INTERPRET-or-fallback now."
            )
        return str(result)

    return StructuredTool.from_function(
        func=guarded_run_validated_python,
        name="run_validated_python",
        description=(
            "Validate and execute a raw Python snippet. The code argument may be "
            "multi-line. Prints stdout/stderr. Rejects identical repeated code."
        ),
    )


def _build_agent(capability_cards: str) -> AgentExecutor:
    llm = get_chat_model(temperature=0.0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        ("human", "Question: {input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ]).partial(capability_cards=capability_cards)
    tools = [_make_guarded_python_tool(), *_BASE_TOOLS]
    agent = create_tool_calling_agent(
        llm=llm,
        tools=tools,
        prompt=prompt,
    )
    return AgentExecutor(
        agent=agent,
        tools=tools,
        max_iterations=8,
        return_intermediate_steps=True,
        verbose=False,
    )


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


def _is_successful_observation(observation: str) -> bool:
    stripped = observation.strip()
    if not stripped or stripped == "(no output)":
        return False
    if stripped.startswith(_SUCCESS_FAILURE_MARKERS):
        return False
    return bool(_NUMBER_RE.search(stripped))


def _successful_execution_observation(steps: list[dict[str, str]]) -> str:
    for step in reversed(steps):
        if step.get("tool") != "run_validated_python":
            continue
        observation = step.get("observation", "")
        if _is_successful_observation(observation):
            return observation.strip()
    return ""


def _parse_output(text: str) -> tuple[str, float, list[str]]:
    """Extract result, confidence, and limitations from agent output."""
    result = ""
    confidence = 0.0
    limitations: list[str] = []
    self_check = ""

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            result = line[len("RESULT:"):].strip()
        elif line.startswith("NUMERIC SELF-CHECK:"):
            self_check = line[len("NUMERIC SELF-CHECK:"):].strip()
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

    if _NUMBER_RE.search(result) and not self_check:
        confidence = min(confidence or 0.3, 0.3)
        limitations.append("unverified scope: numeric self-check missing")

    scope_text = f"{result}\n{self_check}".lower()
    unscoped_markers = ("region x", "{place}", "place_or_address", "location not specified")
    if _NUMBER_RE.search(result) and any(marker in scope_text for marker in unscoped_markers):
        confidence = min(confidence or 0.3, 0.3)
        limitations.append("unverified scope: location is missing or placeholder-like")

    return result, confidence, limitations


def code_spatial_agent_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for CodeSpatialAgent."""
    directives = state.get("orchestrator_directives", [])
    iteration = state.get("iteration", 0)

    my_directives = [d for d in directives if d.get("target_agent") == "CodeSpatialAgent"]
    if not my_directives:
        return {"raw_data": [], "agent_trace": [{"node": "code_spatial_agent", "skipped": True}]}

    new_entries: list[RawEntry] = []
    trace: list[dict[str, Any]] = []

    for directive in my_directives:
        question = directive.get("question", "")
        sq_id = directive.get("sub_query_id", "")
        cards = retrieve_capabilities(question, k=4)
        capability_context = format_capability_cards(cards)
        executor = _build_agent(capability_context)

        try:
            result = executor.invoke({"input": question})
            output_text = result.get("output", "")
            intermediate_steps = _format_steps(result.get("intermediate_steps", []))
            answer, confidence, limitations = _parse_output(output_text)
            fallback_observation = _successful_execution_observation(intermediate_steps)
            if fallback_observation and (not answer or not _NUMBER_RE.search(answer)):
                answer = f"Measured code output: {fallback_observation}"
                confidence = min(max(confidence, 0.3), 0.3)
                limitations = [
                    "The final text did not expose a RESULT field with numbers; "
                    "content is based on the last successful tool output.",
                    "unverified scope: numeric self-check missing",
                ]
            elif not fallback_observation:
                answer = (
                    "CodeSpatialAgent did not complete execution, but this remains "
                    "low-confidence augmentation. Plan: compute named-place network/"
                    "accessibility/POI metrics with osmnx or local data, then use the "
                    "results as quantitative support rather than a gate for the final answer."
                )
                confidence = min(confidence, 0.3)
                limitations = limitations or [
                    "No complete code execution result was available; use as planning "
                    "guidance only."
                ]

            entry = board_message(
                agent="CodeSpatialAgent",
                iteration=iteration,
                msg_type="code_result",
                content=answer
                + (f"\nLimitations: {'; '.join(limitations)}" if limitations else ""),
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
            trace.append({"agent": "CodeSpatialAgent", "sq_id": sq_id, "confidence": confidence})
        except Exception as exc:
            err_entry = board_message(
                agent="CodeSpatialAgent",
                iteration=iteration,
                msg_type="code_result",
                content=(
                    "CodeSpatialAgent low-confidence augmentation only. Execution did "
                    f"not complete: {exc}. Plan: if network/geodata become available, "
                    "compute spatial metrics such as street-network density, POI counts, "
                    "catchment/isochrone accessibility, and distance bands for the named "
                    "location. "
                    "Treat this as missing augmentation, not as evidence against answering."
                ),
                sub_query_id=sq_id,
                confidence=0.3,
            )
            new_entries.append(err_entry)
            trace.append({
                "agent": "CodeSpatialAgent",
                "sq_id": sq_id,
                "confidence": 0.3,
                "partial": True,
            })

    return {
        "raw_data": new_entries,
        "agent_trace": trace,
    }
