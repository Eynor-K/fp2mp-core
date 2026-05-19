"""
NormativeAgent — ReAct agent for regulatory documents and standards.

Search strategy: local RAG first, fallback to web search if < 2 chunks found.
Always includes document name, section, and exact quote in citations.
Confidence threshold for promote is 0.7 (stricter than other agents).
"""

from __future__ import annotations

from typing import Any

from langchain_classic.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard, Citation, RawEntry, board_message
from fp2mp_core.tools.vector_store import normative_vector_search_tool
from fp2mp_core.tools.web_search import fetch_url_tool, normative_web_search_tool

_SYSTEM_PROMPT = """\
You are the NormativeLiteratureAgent. You find regulatory requirements, standards, and rules
relevant to the sub-query.

Available tools: normative_vector_search_tool, normative_web_search_tool, fetch_url_tool.

Strategy:
1. First call normative_vector_search_tool (local database of regulatory documents).
2. If it returns fewer than 2 relevant results, call normative_web_search_tool.
3. For any document found, verify quotes with fetch_url_tool if possible.
4. Always include: document name, section number, exact requirement text.

Confidence:
- 0.9: found in local normative DB with exact quote
- 0.7: found via web search with document name and section
- 0.5: indirect reference, document not directly accessible

You must use the ReAct format exactly. Do not write a final answer until you have
called normative_vector_search_tool at least once.
If the local vector search has insufficient evidence, call normative_web_search_tool.

Use exactly this format:

Question: the input question
Thought: what regulatory source you need to search or verify
Action: one of [{tool_names}]
Action Input: the input to the action
Observation: the tool result
... repeat Thought/Action/Action Input/Observation as needed
Thought: I now know the final answer
Final Answer:
ANSWER: <synthesized regulatory findings>
CONFIDENCE: <0.0-1.0>
SOURCES: <document names and/or URLs>
CITATIONS: <exact quotes from documents>

{tools}

Tool names: {tool_names}

Question: {input}

{agent_scratchpad}"""

_TOOLS = [normative_vector_search_tool, normative_web_search_tool, fetch_url_tool]


def _build_agent() -> AgentExecutor:
    llm = get_chat_model(temperature=0.0)
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
        max_iterations=8,
        handle_parsing_errors=(
            "Use exactly one ReAct step: Thought, Action, Action Input. "
            "Do not write Observation yourself."
        ),
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
    answer = ""
    confidence = 0.5
    citations: list[Citation] = []
    citation_texts: list[str] = []

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
            docs = [d.strip() for d in line[len("SOURCES:"):].split(",") if d.strip()]
            citations = [Citation(document=d) for d in docs]
        elif line.startswith("CITATIONS:"):
            citation_texts.append(line[len("CITATIONS:"):].strip())

    if not answer:
        answer = text[:500]

    if citation_texts:
        for i, cit_text in enumerate(citation_texts):
            if i < len(citations):
                citations[i] = Citation(
                    document=citations[i].get("document", ""),
                    quote=cit_text,
                )
            else:
                citations.append(Citation(quote=cit_text))

    return answer, confidence, citations


def normative_agent_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for NormativeAgent."""
    directives = state.get("orchestrator_directives", [])
    iteration = state.get("iteration", 0)

    my_directives = [d for d in directives if d.get("target_agent") == "NormativeAgent"]
    if not my_directives:
        return {"raw_data": [], "agent_trace": [{"node": "normative_agent", "skipped": True}]}

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

            entry = board_message(
                agent="NormativeAgent",
                iteration=iteration,
                msg_type="normative_findings",
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
            trace.append({"agent": "NormativeAgent", "sq_id": sq_id, "confidence": confidence})
        except Exception as exc:
            err_entry = board_message(
                agent="NormativeAgent",
                iteration=iteration,
                msg_type="normative_findings",
                content=f"NormativeAgent did not produce usable evidence: {exc}",
                sub_query_id=sq_id,
                confidence=0.1,
            )
            new_entries.append(err_entry)

    return {
        "raw_data": new_entries,
        "agent_trace": trace,
    }
