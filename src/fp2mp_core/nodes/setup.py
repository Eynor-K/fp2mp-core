"""
Graph startup nodes: init → redi_decompose → init_blackboard.

These three nodes run once at the beginning of every graph execution
to classify intent, decompose the question via ReDI, and populate
the initial BlackBoard state.
"""

from __future__ import annotations

import uuid
from typing import Any

from fp2mp_core.config import get_settings
from fp2mp_core.nodes.context import classify_question_intent, _modality_to_agent
from fp2mp_core.redi import ReDIDecomposer, ReDIEnricher
from fp2mp_core.state import (
    BlackBoard,
    SubQuery,
    Task,
    WikiPage,
    board_message,
)
from fp2mp_core.wiki.index import build_index
from fp2mp_core.wiki.log import make_initial_log_page


def init_node(state: BlackBoard) -> dict[str, Any]:
    """Set graph defaults before any agent nodes run."""
    settings = get_settings()
    question = state.get("question", "")
    return {
        "question_intent": classify_question_intent(question),
        "redi_decomposition": [],
        "tasks": [],
        "raw_data": [],
        "wiki": {},
        "output": [],
        "iteration": 0,
        "max_iterations": state.get("max_iterations") or settings.max_iterations,
        "stop_flag": False,
        "critique": {},
        "final_answer": None,
        "orchestrator_directives": [],
        "next_action": "dispatch",
        "current_stage": "init",
        "agent_trace": [],
        "errors": [],
        "stagnation_count": 0,
        "progress_delta": 0,
    }


def redi_decompose_node(state: BlackBoard) -> dict[str, Any]:
    """Stage A + B: decompose question into sub-queries and enrich them."""
    question = state["question"]

    decomposer = ReDIDecomposer()
    enricher = ReDIEnricher()

    sub_queries = decomposer(question)
    sub_queries = enricher.enrich_all(sub_queries)

    return {
        "redi_decomposition": sub_queries,
        "current_stage": "redi_done",
        "agent_trace": [
            {
                "node": "redi_decompose",
                "sub_queries": [sq["sub_query_id"] for sq in sub_queries],
            }
        ],
    }


def initialize_blackboard_node(state: BlackBoard) -> dict[str, Any]:
    """Create initial tasks from ReDI sub-queries and set up wiki skeleton."""
    sub_queries: list[SubQuery] = state.get("redi_decomposition", [])

    tasks: list[Task] = []
    for sq in sub_queries:
        modality = sq.get("search_modality", "any")
        agent = _modality_to_agent(modality)
        tasks.append(
            Task(
                task_id=str(uuid.uuid4()),
                sub_query_id=sq["sub_query_id"],
                assigned_agent=agent,
                status="pending",
                priority="high",
                proposer="system",
                created_at_iteration=0,
                directive=sq.get("text", ""),
            )
        )

    log_page = make_initial_log_page()
    index_content = build_index({}, iteration=0)
    index_page = WikiPage(
        page_id="index.md",
        title="Knowledge Index",
        content=index_content,
        updated_by="system",
        updated_at_iteration=0,
        confidence=1.0,
        citations=[],
        tags=["system"],
        incoming_cross_refs=[],
        relevance_score=1.0,
    )

    init_message = board_message(
        agent="system",
        iteration=0,
        msg_type="init",
        content=(
            f"BlackBoard initialised. Question: {state['question']}\n"
            f"Sub-queries: {[sq['sub_query_id'] for sq in sub_queries]}"
        ),
    )

    return {
        "tasks": tasks,
        "wiki": {"index.md": index_page, "log.md": log_page},
        "raw_data": [init_message],
        "current_stage": "blackboard_ready",
        "iteration": 0,
    }
