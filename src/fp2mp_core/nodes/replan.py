"""
redi_replan_node — iterative ReDI re-decomposition (gemini Critic→re-plan pattern).

Sits on the critic "continue" edge before the Orchestrator. When the critic
asks for more work, the decomposition is revised in light of the feedback and
accumulated knowledge instead of staying frozen. Domain-neutral and best-effort:
any failure is a safe pass-through (decomposition unchanged).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger("fp2mp_core.replan")

from fp2mp_core.nodes.context import _modality_to_agent, coverage_from_sub_queries, wiki_briefing
from fp2mp_core.redi import ReDIReplanner
from fp2mp_core.state import BlackBoard, SubQuery, Task

_replanner = ReDIReplanner()


def redi_replan_node(state: BlackBoard) -> dict[str, Any]:
    """Revise the ReDI decomposition from critic feedback. No-op on failure."""
    critique = state.get("critique", {})
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 6) or 6

    # Only re-plan when the critic genuinely wants more work, and only while
    # there is still a round left to act on any new sub-queries.
    if critique.get("action") != "CONTINUE" or critique.get("force_stop", False):
        return {}
    if iteration >= max_iterations - 1:
        return {}

    current: list[SubQuery] = state.get("redi_decomposition", [])
    if not current:
        return {}

    coverage = coverage_from_sub_queries(state)
    # If everything is already covered there is nothing to re-plan.
    if coverage and all(v == "covered" for v in coverage.values()):
        return {}

    old_ids = {sq.get("sub_query_id", "") for sq in current}

    try:
        updated = _replanner(
            question=state.get("question", ""),
            current_sub_queries=current,
            coverage=coverage,
            feedback=critique.get("reasoning", ""),
            briefing=wiki_briefing(state, limit=1200),
        )
    except Exception as exc:
        logger.info("iter=%d replan skipped (error: %s)", iteration, exc)
        return {}

    if not updated:
        return {}

    # Never lose a covered sub-query the re-planner dropped.
    updated_ids = {sq["sub_query_id"] for sq in updated}
    for sq in current:
        sid = sq.get("sub_query_id", "")
        if sid not in updated_ids and coverage.get(sid) == "covered":
            updated.append(sq)
            updated_ids.add(sid)

    new_ids = updated_ids - old_ids
    # No structural change → pass through (avoid churn).
    if not new_ids:
        return {}

    new_tasks: list[Task] = []
    sq_by_id = {sq["sub_query_id"]: sq for sq in updated}
    for sid in new_ids:
        sq = sq_by_id[sid]
        new_tasks.append(
            Task(
                task_id=str(uuid.uuid4()),
                sub_query_id=sid,
                assigned_agent=_modality_to_agent(sq.get("search_modality", "any")),
                status="pending",
                priority="high",
                proposer="ReDIReplanner",
                created_at_iteration=iteration,
                directive=sq.get("text", ""),
            )
        )

    logger.info(
        "iter=%d replan | %d → %d sub-queries (+%d new)",
        iteration, len(current), len(updated), len(new_ids),
    )
    return {
        "redi_decomposition": updated,
        "tasks": new_tasks,
        "current_stage": "replanned",
        "agent_trace": [
            {
                "node": "redi_replan",
                "iteration": iteration,
                "new_sub_queries": sorted(new_ids),
                "total_sub_queries": len(updated),
            }
        ],
    }
