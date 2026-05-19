"""
WebSearchAgent — ReAct agent for general internet search.

Receives a directive from the Orchestrator (via state["orchestrator_directives"]),
performs up to 3 search iterations, and writes a RawEntry to raw_data.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool, StructuredTool

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard, Citation, RawEntry, board_message
from fp2mp_core.tools.web_search import research_url_tool, web_search_tool

_SYSTEM_PROMPT = """\
You are the WebSearchAgent. Your task is to find information that answers one specific sub-query.

Available tools: web_search_tool, research_url_tool.

Instructions:
1. Analyze the sub-query and form a precise search query.
2. Call web_search_tool. If results are insufficient, refine the query and search again
   (max 3 searches).
3. Triage the ranked results. Select only the top 1-3 most relevant URLs for deep-read.
4. Call research_url_tool for selected URLs with the sub-query as focus. Do not deep-read
   every result. If a fetch fails, use the snippet as fallback and do not retry the same URL.
5. Synthesize a clear, factual answer with inline source URLs, direct quotes/numbers where
   available, and confidence score.

Confidence scoring:
- 0.8+: multiple corroborating sources with direct evidence
- 0.6-0.8: single credible source or indirect evidence
- below 0.6: uncertain or proxy evidence

You must use the ReAct format exactly. Do not write a final answer until you have called
web_search_tool at least once and attempted research_url_tool on at least one credible URL,
unless all URLs are invalid or search fails.

Finish with this exact contract:
ANSWER: <your synthesized answer>
CONFIDENCE: <0.0-1.0>
SOURCES: <comma-separated URLs>

Do not write artificial Observation text. Tool outputs arrive automatically.
"""

_URL_RE = re.compile(r"https?://[^\s,)>\]]+")


def _make_guarded_research_tool() -> BaseTool:
    seen_urls: set[str] = set()

    def guarded_research_url_tool(url: str, focus: str = "") -> str:
        """Deep-read at most 3 unique URLs for this sub-query."""
        normalized = _normalize_for_guard(url)
        if normalized in seen_urls:
            return (
                "deep-read skipped: URL already researched. "
                "Synthesize from gathered evidence now."
            )
        if len(seen_urls) >= 3:
            return "deep-read limit reached: synthesize from gathered evidence now."
        seen_urls.add(normalized)
        return research_url_tool.invoke({"url": url, "focus": focus})

    return StructuredTool.from_function(
        func=guarded_research_url_tool,
        name="research_url_tool",
        description=(
            "Fetch, extract, and distill one URL for a focus/sub-query. "
            "Limited to 3 unique URLs per sub-query."
        ),
    )


def _build_agent() -> AgentExecutor:
    llm = get_chat_model(temperature=0.1)
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        ("human", "Question: {input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
    tools = [web_search_tool, _make_guarded_research_tool()]
    agent = create_tool_calling_agent(
        llm=llm,
        tools=tools,
        prompt=prompt,
    )
    return AgentExecutor(
        agent=agent,
        tools=tools,
        max_iterations=6,
        return_intermediate_steps=True,
        verbose=False,
    )


def _normalize_for_guard(url: str) -> str:
    return str(url or "").strip().strip("'\"").strip().rstrip("/").lower()


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


def _last_research_observation(steps: list[dict[str, str]]) -> str:
    for step in reversed(steps):
        if step.get("tool") != "research_url_tool":
            continue
        observation = step.get("observation", "").strip()
        failed = observation.startswith(("Research fetch failed", "Failed to research"))
        if observation and not failed:
            return observation
    return ""


def _parse_output(text: str, steps: list[dict[str, str]]) -> tuple[str, float, list[Citation]]:
    """Extract answer, confidence, and citations from agent output."""
    answer = ""
    confidence = 0.0
    confidence_reported = False
    citations: list[Citation] = []

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ANSWER:"):
            answer = line[len("ANSWER:"):].strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line[len("CONFIDENCE:"):].strip())
                confidence_reported = True
            except ValueError:
                pass
        elif line.startswith("SOURCES:"):
            urls = [u.strip() for u in line[len("SOURCES:"):].split(",") if u.strip()]
            citations = [Citation(url=u) for u in urls]

    if not answer:
        answer = _last_research_observation(steps) or text[:500]

    if not confidence_reported:
        confidence = min(confidence or 0.4, 0.4)
        answer += "\nLimitations: confidence not reported by WebSearchAgent."

    if not citations:
        urls = _URL_RE.findall(answer)
        citations = [Citation(url=url.rstrip(".")) for url in urls]

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
            intermediate_steps = _format_steps(result.get("intermediate_steps", []))
            answer, confidence, citations = _parse_output(output_text, intermediate_steps)

            entry = board_message(
                agent="WebSearchAgent",
                iteration=iteration,
                msg_type="web_findings",
                content=answer,
                sub_query_id=sq_id,
                confidence=confidence,
                citations=citations,
                tool_trace=[{
                    "directive": directive,
                    "raw_output": output_text[:300],
                    "intermediate_steps": intermediate_steps,
                }],
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
