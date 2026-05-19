"""
BlackBoard initialization node and helper utilities.

All agents read from the BlackBoard via the helper functions defined here.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fp2mp_core.config import get_settings
from fp2mp_core.redi import ReDIDecomposer, ReDIEnricher
from fp2mp_core.state import BlackBoard, SubQuery, Task, WikiPage, board_message
from fp2mp_core.wiki.index import build_index
from fp2mp_core.wiki.log import make_initial_log_page

# ---------------------------------------------------------------------------
# Initial state node (START -> init -> redi_decompose)
# ---------------------------------------------------------------------------


def init_node(state: BlackBoard) -> dict[str, Any]:
    """Set graph defaults before any agent nodes run."""
    settings = get_settings()
    return {
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
        "synthesis_refine": state.get("synthesis_refine", settings.synthesis_refine),
    }


# ---------------------------------------------------------------------------
# ReDI decomposition node (init -> redi_decompose -> init_blackboard)
# ---------------------------------------------------------------------------


def redi_decompose_node(state: BlackBoard) -> dict[str, Any]:
    """Stage A + B: decompose question into sub-queries and enrich them."""
    question = state["question"]

    decomposer = ReDIDecomposer()
    enricher = ReDIEnricher()

    sub_queries = decomposer(question)
    sub_queries = enricher.enrich_all(sub_queries)
    sub_queries = _ensure_code_spatial_sub_query(question, sub_queries)

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


# ---------------------------------------------------------------------------
# BlackBoard initialization node
# ---------------------------------------------------------------------------


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

    # Stub wiki with index and log
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


# ---------------------------------------------------------------------------
# Helpers used by all nodes
# ---------------------------------------------------------------------------


def _modality_to_agent(modality: str) -> str:
    return {
        "web": "WebSearchAgent",
        "normative": "NormativeAgent",
        "code": "CodeSpatialAgent",
        "any": "CodeSpatialAgent",
    }.get(modality, "CodeSpatialAgent")


def _ensure_code_spatial_sub_query(question: str, sub_queries: list[SubQuery]) -> list[SubQuery]:
    """Ensure the pipeline gets at least one quantitative spatial augmentation attempt."""
    if any(sq.get("search_modality") == "code" for sq in sub_queries):
        return sub_queries

    place = _extract_named_place(question)
    if place:
        text = (
            f"Compute one or more quantitative/spatial indicators for {place} "
            "and explain how they should inform the answer. Use only this explicit "
            f"place as the spatial scope. Original question: {question}"
        )
    else:
        text = (
            "Identify quantitative indicators relevant to the question, but do not "
            "invent a concrete place. If a calculation requires a location, state "
            "LIMITATIONS: location not specified and provide a low-confidence generic "
            f"measurement plan instead. Original question: {question}"
        )

    code_sq = SubQuery(
        sub_query_id="sq_code_support",
        text=text,
        intent_aspect="quantitative spatial support",
        search_modality="code",
        independence=True,
        enriched_variants=[],
        keywords=[],
        domain_hints=[],
    )
    return [code_sq, *sub_queries]


def _extract_named_place(question: str) -> str | None:
    """Extract benchmark-style leading place: '<location>. <question>' conservatively."""
    first, sep, rest = question.strip().partition(".")
    if not sep or not rest.strip():
        return None

    candidate = first.strip(" \t\n\r'\"")
    if not candidate or len(candidate) > 80 or "?" in candidate:
        return None

    lower = candidate.lower()
    question_starts = (
        "what ",
        "how ",
        "why ",
        "when ",
        "where ",
        "which ",
        "should ",
        "can ",
        "is ",
        "are ",
        "какие ",
        "как ",
        "почему ",
        "где ",
        "нужно ",
    )
    if lower.startswith(question_starts):
        return None

    words = re.findall(r"[\wА-Яа-яЁё-]+", candidate)
    if not words or len(words) > 8:
        return None

    has_location_hint = "," in candidate or any(
        token in lower
        for token in (
            "city",
            "district",
            "region",
            "oblast",
            "район",
            "город",
            "область",
            "край",
            "республика",
        )
    )
    has_capitalized_word = any(word[:1].isupper() for word in words)
    if has_location_hint or (has_capitalized_word and len(words) <= 5):
        return candidate
    return None


def wiki_briefing(state: BlackBoard, limit: int = 3000) -> str:
    """
    Compact context string for agents: index + top output facts + raw tail.
    Used by Orchestrator, Mediator, and Critic to avoid reading the full wiki.
    """
    parts: list[str] = []

    # Index
    index_page = state.get("wiki", {}).get("index.md")
    if index_page:
        parts.append("## Wiki Index\n" + index_page.get("content", "")[:800])

    # Confirmed facts
    output = state.get("output", [])
    if output:
        parts.append("\n## Confirmed Facts")
        for f in output[:10]:
            claim = f.get("claim", "")
            conf = f.get("confidence", 0.0)
            sq = f.get("sub_query_id", "?")
            parts.append(f"- [{sq} conf={conf:.2f}] {claim[:200]}")

    # Recent raw_data tail
    raw = state.get("raw_data", [])
    recent = [r for r in raw if not r.get("curated", False)][-5:]
    if recent:
        parts.append("\n## Recent Raw Entries (not yet curated)")
        for r in recent:
            agent = r.get("agent", "?")
            rtype = r.get("type", "?")
            snippet = r.get("content", "")[:200].replace("\n", " ")
            parts.append(f"- [{agent}/{rtype}] {snippet}")

    text = "\n".join(parts)
    return text[:limit]


def coverage_from_sub_queries(state: BlackBoard) -> dict[str, str]:
    """Build coverage map: sub_query_id → "covered" | "partial" | "pending"."""
    sub_queries = state.get("redi_decomposition", [])
    output = state.get("output", [])

    coverage: dict[str, str] = {}
    for sq in sub_queries:
        sq_id = sq["sub_query_id"]
        relevant = [f for f in output if f.get("sub_query_id") == sq_id]
        if not relevant:
            coverage[sq_id] = "pending"
        elif max((f.get("confidence", 0.0) for f in relevant), default=0.0) >= 0.7:
            coverage[sq_id] = "covered"
        else:
            coverage[sq_id] = "partial"
    return coverage
