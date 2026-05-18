"""
LangGraph StateGraph assembly.

Graph topology:
  START → init → redi_decompose → init_blackboard → orchestrator
                                                      │
          ┌────[Send]──► web_search_agent ─────┤
          ├────[Send]──► normative_agent ───────┤   → wiki_curator
          ├────[Send]──► code_spatial_agent ────┤
          └────[Send]──► mediator ──────────────┘

  wiki_curator ──continue──► orchestrator
               ──hypothesis─► hypothesis ─► critic
               ──critic────► critic
               ──finish_ready─► answer_commit

  critic ──continue──► redi_replan ─► orchestrator
         ──finish────► answer_commit

  answer_commit → final_synthesis → END
  (answer_commit = gemini-style committed weighted answer; synthesis formats it)
"""

from __future__ import annotations

import logging
import tiktoken
from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger("fp2mp_core")
from langgraph.graph import END, START, StateGraph

from fp2mp_core.config import get_settings
from fp2mp_core.llm import set_active_model
from fp2mp_core.nodes.agents.blocksnet import blocksnet_agent_node
from fp2mp_core.nodes.agents.code_spatial import code_spatial_agent_node
from fp2mp_core.nodes.agents.normative import normative_agent_node
from fp2mp_core.nodes.agents.web_search import web_search_agent_node
from fp2mp_core.nodes.setup import init_node, initialize_blackboard_node, redi_decompose_node
from fp2mp_core.nodes.critic import critic_node, route_from_critic
from fp2mp_core.nodes.hypothesis import hypothesis_node
from fp2mp_core.nodes.curator import route_from_curator, wiki_curator_node
from fp2mp_core.nodes.mediator import mediator_node
from fp2mp_core.nodes.orchestrator import orchestrator_node, route_from_orchestrator
from fp2mp_core.nodes.answer import answer_commit_node
from fp2mp_core.nodes.replan import redi_replan_node
from fp2mp_core.nodes.synthesis import final_synthesis_node
from fp2mp_core.state import BaseState, BlackBoard, create_initial_state

_enc = tiktoken.get_encoding("cl100k_base")


def _ai_message(content: str, name: str) -> AIMessage:
    n = len(_enc.encode(content))
    return AIMessage(
        content=content,
        name=name,
        usage_metadata={"input_tokens": 0, "output_tokens": n, "total_tokens": n},
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
    graph.add_node("hypothesis", hypothesis_node)
    graph.add_node("critic", critic_node)
    graph.add_node("redi_replan", redi_replan_node)
    graph.add_node("answer_commit", answer_commit_node)
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
            "hypothesis": "hypothesis",
            "critic": "critic",
            "finish_ready": "answer_commit",
        },
    )

    # hypothesis always feeds critic (hypotheses create tasks, critic decides next steps)
    graph.add_edge("hypothesis", "critic")

    # --- Critic routing ---
    graph.add_conditional_edges(
        "critic",
        route_from_critic,
        {
            "continue": "redi_replan",
            "finish": "answer_commit",
        },
    )

    # Iterative ReDI: re-decompose from critic feedback, then re-orchestrate.
    graph.add_edge("redi_replan", "orchestrator")

    # gemini pattern: commit a concrete weighted answer, then format it.
    graph.add_edge("answer_commit", "final_synthesis")
    graph.add_edge("final_synthesis", END)

    return graph.compile()


def _build_log(input: str, raw_data: list, final_answer: str) -> list:
    log: list = [HumanMessage(content=input)]

    entries = sorted(raw_data, key=lambda e: (e.get("iteration", 0), e.get("entry_id", "")))

    for entry in entries:
        agent = entry.get("agent", "agent")
        tool_trace = entry.get("tool_trace") or []

        for trace in tool_trace:
            directive = trace.get("directive") or {}
            question = directive.get("question") or directive.get("directive", "")
            if question:
                log.append(HumanMessage(content=question))

            for step in trace.get("intermediate_steps") or []:
                tool_name = step.get("tool", "tool")
                tool_input = str(step.get("tool_input", ""))
                observation = str(step.get("observation", ""))
                log.append(HumanMessage(content=f"[{tool_name}] {tool_input}"))
                if observation:
                    log.append(_ai_message(observation, tool_name))

            raw_output = trace.get("raw_output", "")
            if raw_output:
                log.append(_ai_message(raw_output, agent))

        has_data = any(
            t.get("intermediate_steps") or t.get("raw_output")
            for t in tool_trace
        )
        if not tool_trace or not has_data:
            content = entry.get("content", "")
            if content:
                log.append(_ai_message(content, agent))

    log.append(_ai_message(final_answer, "FinalSynthesis"))
    return log


def run(input: str, model: str, max_iterations: int | None = None) -> BaseState:
    """Run the graph and return the fp2mp-baselines/eval state format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("run | model=%s max_iter=%s | question: %s", model, max_iterations, input[:80])
    set_active_model(model)
    compiled = build_graph()
    initial = create_initial_state(input, max_iterations=max_iterations)
    # Each loop iteration spans ~7 supersteps (orchestrator → agents → curator
    # → hypothesis/critic → replan). Scale the recursion limit to the iteration
    # budget so the loop terminates via the critic / max_iterations, not via a
    # premature GraphRecursionError.
    effective_max = max_iterations or get_settings().max_iterations
    recursion_limit = max(50, effective_max * 9 + 15)
    result = compiled.invoke(initial, config={"recursion_limit": recursion_limit})

    final_answer = result.get("final_answer") or ""
    raw_data = result.get("raw_data", [])

    return BaseState(
        input=input,
        output=final_answer,
        log=_build_log(input, raw_data, final_answer),
    )
