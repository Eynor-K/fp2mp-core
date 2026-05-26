"""
LangGraph StateGraph assembly.

Graph topology:
  START → init → redi_decompose → init_blackboard → orchestrator
                                                      │
          ┌────[Send]──► web_search_agent ─────┤
          ├────[Send]──► normative_agent ───────┤   → wiki_curator
          ├────[Send]──► code_spatial_agent ────┤
          ├────[Send]──► blocksnet_agent ───────┤
          └────[Send]──► mediator ──────────────┘

  wiki_curator ──continue──► orchestrator
               ──critic────► critic
               ──finish_ready─► final_synthesis

  critic ──continue──► orchestrator
         ──finish────► final_synthesis

  final_synthesis → END
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from fp2mp_core.llm import set_active_model
from fp2mp_core.nodes.agents.blocksnet import blocksnet_agent_node
from fp2mp_core.nodes.agents.code_spatial import code_spatial_agent_node
from fp2mp_core.nodes.agents.normative import normative_agent_node
from fp2mp_core.nodes.agents.web_search import web_search_agent_node
from fp2mp_core.nodes.blackboard import init_node, initialize_blackboard_node, redi_decompose_node
from fp2mp_core.nodes.critic import critic_node, route_from_critic
from fp2mp_core.nodes.curator import route_from_curator, wiki_curator_node
from fp2mp_core.nodes.mediator import mediator_node
from fp2mp_core.nodes.orchestrator import orchestrator_node, route_from_orchestrator
from fp2mp_core.nodes.synthesis import final_synthesis_node
from fp2mp_core.config import get_settings
from fp2mp_core.runtime import start_run, write_logs
from fp2mp_core.state import BaseState, BlackBoard, create_initial_state
from fp2mp_core.usage import TokenUsageAccumulator, count_tokens


def _ai_message(content: str, name: str, input_tokens: int = 0) -> AIMessage:
    """Build an AIMessage carrying a per-message tiktoken estimate.

    ``output_tokens`` is the cl100k token count of ``content``; ``input_tokens``
    is the estimated size of the prompt this message responded to (passed in by
    the log builder). These are best-effort per-message estimates for display
    and rough summation — the authoritative run total lives in ``BaseState.usage``.
    """
    output_tokens = count_tokens(content)
    return AIMessage(
        content=content,
        name=name,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def build_graph():
    """Build and compile the multi-agent QA graph."""
    graph = StateGraph(BlackBoard)

    # --- Nodes ---
    graph.add_node("init", init_node)
    graph.add_node("redi_decompose", redi_decompose_node)
    graph.add_node("init_blackboard", initialize_blackboard_node)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("web_search_agent", web_search_agent_node)
    graph.add_node("normative_agent", normative_agent_node)
    graph.add_node("code_spatial_agent", code_spatial_agent_node)
    graph.add_node("blocksnet_agent", blocksnet_agent_node)
    graph.add_node("mediator", mediator_node)
    graph.add_node("wiki_curator", wiki_curator_node)
    graph.add_node("critic", critic_node)
    graph.add_node("final_synthesis", final_synthesis_node)

    # --- Linear start sequence ---
    graph.add_edge(START, "init")
    graph.add_edge("init", "redi_decompose")
    graph.add_edge("redi_decompose", "init_blackboard")
    graph.add_edge("init_blackboard", "orchestrator")

    # --- Orchestrator fan-out (parallel Send or direct critic routing) ---
    graph.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        {
            "web_search_agent": "web_search_agent",
            "normative_agent": "normative_agent",
            "code_spatial_agent": "code_spatial_agent",
            "blocksnet_agent": "blocksnet_agent",
            "mediator": "mediator",
            "critic": "critic",
        },
    )

    # --- Fan-in: all active agents → wiki_curator ---
    for node in ["web_search_agent", "normative_agent", "code_spatial_agent", "blocksnet_agent"]:
        graph.add_edge(node, "wiki_curator")

    # Mediator also feeds curator (mediator results need indexing)
    graph.add_edge("mediator", "wiki_curator")

    # --- WikiCurator routing ---
    graph.add_conditional_edges(
        "wiki_curator",
        route_from_curator,
        {
            "continue": "orchestrator",
            "critic": "critic",
            "finish_ready": "final_synthesis",
        },
    )

    # --- Critic routing ---
    graph.add_conditional_edges(
        "critic",
        route_from_critic,
        {
            "continue": "orchestrator",
            "finish": "final_synthesis",
        },
    )

    graph.add_edge("final_synthesis", END)

    return graph.compile()


def _build_log(input: str, raw_data: list, final_answer: str) -> list:
    """Reconstruct a LangChain message log from raw_data.

    Each AIMessage carries a per-message tiktoken estimate: ``output_tokens`` is
    the size of its own content, ``input_tokens`` the size of the most recent
    preceding prompt (the human turn / tool call it responded to). These are
    rough estimates for display and summation; the authoritative run total is
    returned separately in ``BaseState.usage``.
    """
    log: list = [HumanMessage(content=input)]
    # Tokens of the most recent "prompt" turn, attributed as input to the next
    # AIMessage we emit.
    last_input_tokens = count_tokens(input)

    entries = sorted(raw_data, key=lambda e: (e.get("iteration", 0), e.get("entry_id", "")))

    for entry in entries:
        agent = entry.get("agent", "agent")
        tool_trace = entry.get("tool_trace") or []

        for trace in tool_trace:
            directive = trace.get("directive") or {}
            question = directive.get("question") or directive.get("directive", "")
            if question:
                log.append(HumanMessage(content=question))
                last_input_tokens = count_tokens(question)

            for step in trace.get("intermediate_steps") or []:
                tool_name = step.get("tool", "tool")
                tool_input = str(step.get("tool_input", ""))
                observation = str(step.get("observation", ""))
                tool_call = f"[{tool_name}] {tool_input}"
                log.append(HumanMessage(content=tool_call))
                last_input_tokens = count_tokens(tool_call)
                if observation:
                    log.append(_ai_message(observation, tool_name, input_tokens=last_input_tokens))

            raw_output = trace.get("raw_output", "")
            if raw_output:
                log.append(_ai_message(raw_output, agent, input_tokens=last_input_tokens))

        if not tool_trace:
            content = entry.get("content", "")
            if content:
                log.append(_ai_message(content, agent, input_tokens=last_input_tokens))

    log.append(_ai_message(final_answer, "FinalSynthesis", input_tokens=last_input_tokens))
    return log


def run(input: str, model: str, max_iterations: int | None = None) -> BaseState:
    """Run the graph and return the fp2mp-baselines/eval state format."""
    set_active_model(model)
    settings = get_settings()
    ctx = start_run(settings.model_output_dir)
    compiled = build_graph()
    initial = create_initial_state(
        input,
        max_iterations=max_iterations,
        run_id=ctx.run_id,
        run_output_dir=str(ctx.run_dir),
    )
    usage = TokenUsageAccumulator()
    result = compiled.invoke(initial, config={"callbacks": [usage]})

    final_answer = result.get("final_answer") or ""
    raw_data = result.get("raw_data", [])
    tool_calls = _collect_tool_calls(raw_data)
    agent_outputs = _collect_agent_outputs(raw_data, result.get("output", []))
    artifact_payload = write_logs(ctx, input, model, final_answer, tool_calls, agent_outputs)

    return BaseState(
        input=input,
        output=final_answer,
        log=_build_log(input, raw_data, final_answer),
        usage=usage.totals(),
        artifacts={
            "run_dir": str(ctx.run_dir),
            "files": artifact_payload.get("saved_files", []),
            "log_md": artifact_payload.get("log_md", str(ctx.run_dir / "run_log.md")),
            "log_json": artifact_payload.get("log_json", str(ctx.run_dir / "run_log.json")),
        },
    )


def _collect_tool_calls(raw_data: list) -> list[dict]:
    calls: list[dict] = []
    entries = sorted(raw_data, key=lambda e: (e.get("iteration", 0), e.get("entry_id", "")))
    for entry in entries:
        agent = entry.get("agent", "")
        for trace in entry.get("tool_trace") or []:
            for step in trace.get("intermediate_steps") or []:
                calls.append({
                    "agent": agent,
                    "tool": step.get("tool", ""),
                    "args": step.get("tool_input", ""),
                    "observation": str(step.get("observation", ""))[:1000],
                })
    return calls


def _collect_agent_outputs(raw_data: list, output_facts: list | None = None) -> list[dict]:
    """Collect final structured agent outputs for audit logs.

    Tool traces show what was called; this preserves what the agent concluded
    (ANALYSIS PLAN / RESULT / HYPOTHESES / etc.) so runs can be audited without
    reconstructing conclusions from tool observations.
    """
    promoted_entry_ids = {
        str(f.get("source_entry_id", ""))
        for f in output_facts or []
        if f.get("source_entry_id")
    }
    promoted_claims = [str(f.get("claim", "")) for f in output_facts or []]
    content_types = {"blocksnet_result", "code_result", "mediator_synthesis"}
    outputs: list[dict] = []
    entries = sorted(raw_data, key=lambda e: (e.get("iteration", 0), e.get("entry_id", "")))
    for entry in entries:
        content = str(entry.get("content", "") or "").strip()
        entry_type = entry.get("type", "")
        if not content or entry_type not in content_types:
            continue
        entry_id = str(entry.get("entry_id", ""))
        promoted = entry_id in promoted_entry_ids or any(
            content[:200] in claim or claim[:200] in content
            for claim in promoted_claims
            if claim
        )
        outputs.append({
            "entry_id": entry_id,
            "agent": entry.get("agent", ""),
            "sub_query_id": entry.get("sub_query_id", ""),
            "iteration": entry.get("iteration", 0),
            "type": entry_type,
            "confidence": entry.get("confidence", 0.0),
            "promotion": "promoted" if promoted else "not_promoted",
            "content": content,
        })
    return outputs
