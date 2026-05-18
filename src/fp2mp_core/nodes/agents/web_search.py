"""
WebSearchAgent — ReAct agent for general internet search.

Receives a directive from the Orchestrator (via state["orchestrator_directives"]),
performs up to 3 search iterations, and writes a RawEntry to raw_data.
"""

from __future__ import annotations

from typing import Any

from langchain_classic.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate

from fp2mp_core.failure import assess_confidence
from fp2mp_core.llm import get_chat_model
from fp2mp_core.nodes.context import parse_follow_ups
from fp2mp_core.state import BlackBoard, Citation, RawEntry, board_message
from fp2mp_core.tools.web_search import fetch_url_tool, web_search_tool

_SYSTEM_PROMPT = """\
You are the WebSearchAgent. Your task is to find information that answers one specific sub-query.

Available tools: web_search_tool, fetch_url_tool.

Instructions:
1. Analyze the sub-query and form a precise search query.
2. Call web_search_tool. If results are insufficient, refine the query and search again (max 3 searches).
3. For the most relevant result, optionally call fetch_url_tool to get more context.
4. Synthesize a clear, factual answer to the sub-query with confidence score.

Confidence scoring:
- 0.8+: multiple corroborating sources with direct evidence
- 0.6-0.8: single credible source or indirect evidence
- below 0.6: uncertain or proxy evidence

CONTEXT USAGE:
- The input may contain ORIGINAL QUESTION, YOUR SUB-TASK and a CONTEXT block with
  findings from other agents. Focus on YOUR SUB-TASK, but use the CONTEXT to build
  on prior findings and avoid repeating work already done. Keep the ORIGINAL
  QUESTION in mind so your answer is actually useful for it.
- OPTIONAL: if another agent should do a specific concrete next step, add one
  line after your final answer:
  FOLLOW_UP: <WebSearchAgent|NormativeAgent|CodeSpatialAgent|BlocksNetAgent> | <task>

You must use the ReAct format exactly. Do not write a final answer until you have called web_search_tool at least once.

Use exactly this format:

Question: the input question
Thought: what you need to search or verify
Action: one of [{tool_names}]
Action Input: the input to the action
Observation: the tool result
... repeat Thought/Action/Action Input/Observation as needed
Thought: I now know the final answer
Final Answer:
ANSWER: <your synthesized answer>
CONFIDENCE: <0.0-1.0>
SOURCES: <comma-separated URLs>

{tools}

Tool names: {tool_names}

Question: {input}

{agent_scratchpad}"""

_TOOLS = [web_search_tool, fetch_url_tool]


def _build_agent() -> AgentExecutor:
    llm = get_chat_model(temperature=0.1)
    prompt = PromptTemplate.from_template(_SYSTEM_PROMPT)
    agent = create_react_agent(
        llm,
        _TOOLS,
        prompt,
        stop_sequence=True,
    )
    return AgentExecutor(
        agent=agent,
        tools=_TOOLS,
        max_iterations=6,
        handle_parsing_errors="Use exactly one ReAct step: Thought, Action, Action Input. Do not write Observation yourself.",
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


def _parse_output(text: str) -> tuple[str, float, list[Citation]]:
    """Extract answer, confidence, and citations from agent output."""
    answer = ""
    confidence = 0.5
    citations: list[Citation] = []

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ANSWER:"):
            answer = line[len("ANSWER:"):].strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line[len("CONFIDENCE:"):].strip())
            except ValueError:
                pass
        elif line.startswith("SOURCES:"):
            urls = [u.strip() for u in line[len("SOURCES:"):].split(",") if u.strip()]
            citations = [Citation(url=u) for u in urls]

    if not answer:
        answer = text[:500]

    return answer, confidence, citations


def web_search_agent_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for WebSearchAgent."""
    directives = state.get("orchestrator_directives", [])
    iteration = state.get("iteration", 0)

    # Find directives for this agent
    my_directives = [d for d in directives if d.get("target_agent") == "WebSearchAgent"]
    if not my_directives:
        return {"raw_data": [], "agent_trace": [{"node": "web_search_agent", "skipped": True}]}

    executor = _build_agent()
    new_entries: list[RawEntry] = []
    trace: list[dict[str, Any]] = []

    for directive in my_directives:
        question = directive.get("question", "")
        sq_id = directive.get("sub_query_id", "")

        try:
            result = executor.invoke({"input": question})
            output_text = result.get("output", "")
            answer, confidence, citations = _parse_output(output_text)
            intermediate_steps = _format_steps(result.get("intermediate_steps", []))

            tool_trace = [{"directive": directive, "raw_output": output_text[:300], "intermediate_steps": intermediate_steps}]
            confidence, failed = assess_confidence(output_text, tool_trace, confidence)
            follow_ups: list[dict[str, str]] = []
            if failed:
                answer = (
                    "[FAILED] WebSearchAgent did not produce a reliable answer. "
                    f"Raw output: {output_text[:400]}"
                )
            else:
                follow_ups = parse_follow_ups(output_text)

            entry = board_message(
                agent="WebSearchAgent",
                iteration=iteration,
                msg_type="web_findings",
                content=answer,
                sub_query_id=sq_id,
                confidence=confidence,
                citations=citations,
                tool_trace=tool_trace,
                follow_up_suggestions=follow_ups,
            )
            new_entries.append(entry)
            trace.append({"agent": "WebSearchAgent", "sq_id": sq_id, "confidence": confidence})
        except Exception as exc:
            err_entry = board_message(
                agent="WebSearchAgent",
                iteration=iteration,
                msg_type="web_findings",
                content=f"Search failed: {exc}",
                sub_query_id=sq_id,
                confidence=0.1,
            )
            new_entries.append(err_entry)

    return {
        "raw_data": new_entries,
        "agent_trace": trace,
    }
