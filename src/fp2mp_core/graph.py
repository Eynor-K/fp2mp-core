"""
LangGraph StateGraph assembly.

Graph topology:
  START → redi_decompose → init_blackboard → orchestrator
                                               │
          ┌────[Send]──► web_search_agent ─────┤
          ├────[Send]──► normative_agent ───────┤   → wiki_curator
          ├────[Send]──► code_spatial_agent ────┤
          └────[Send]──► mediator ──────────────┘

  wiki_curator ──continue──► orchestrator
               ──critic────► critic
               ──finish_ready─► final_synthesis

  critic ──continue──► orchestrator
         ──finish────► final_synthesis

  final_synthesis → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from fp2mp_core.nodes.blackboard import initialize_blackboard_node, redi_decompose_node
from fp2mp_core.nodes.agents.code_spatial import code_spatial_agent_node
from fp2mp_core.nodes.agents.normative import normative_agent_node
from fp2mp_core.nodes.agents.web_search import web_search_agent_node
from fp2mp_core.nodes.critic import critic_node, route_from_critic
from fp2mp_core.nodes.curator import route_from_curator, wiki_curator_node
from fp2mp_core.nodes.mediator import mediator_node
from fp2mp_core.nodes.orchestrator import orchestrator_node, route_from_orchestrator
from fp2mp_core.nodes.synthesis import final_synthesis_node
from fp2mp_core.state import BlackBoard


def build_graph():
    """Build and compile the multi-agent QA graph."""
    graph = StateGraph(BlackBoard)

    # --- Nodes ---
    graph.add_node("redi_decompose", redi_decompose_node)
    graph.add_node("init_blackboard", initialize_blackboard_node)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("web_search_agent", web_search_agent_node)
    graph.add_node("normative_agent", normative_agent_node)
    graph.add_node("code_spatial_agent", code_spatial_agent_node)
    graph.add_node("mediator", mediator_node)
    graph.add_node("wiki_curator", wiki_curator_node)
    graph.add_node("critic", critic_node)
    graph.add_node("final_synthesis", final_synthesis_node)

    # --- Linear start sequence ---
    graph.add_edge(START, "redi_decompose")
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
            "mediator": "mediator",
            "critic": "critic",
        },
    )

    # --- Fan-in: all active agents → wiki_curator ---
    for node in ["web_search_agent", "normative_agent", "code_spatial_agent"]:
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


def run(question: str, max_iterations: int = 6) -> dict:
    """Convenience entry point."""
    from fp2mp_core.state import create_initial_state

    compiled = build_graph()
    initial = create_initial_state(question, max_iterations=max_iterations)
    result = compiled.invoke(initial)
    return result
