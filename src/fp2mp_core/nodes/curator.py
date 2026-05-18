"""
WikiCuratorAgent — central fan-in node after all agents.

Responsibilities:
1. Process uncurated raw_data → create/update WikiPages
2. Run ReDI Fusion on new entries
3. Apply wiki maintenance: pruning, merging, relevance scoring
4. Promote high-confidence content to output (confirmed facts)
5. Update index.md and log.md
6. Detect stagnation (no new entries)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger("fp2mp_core.curator")

from fp2mp_core.config import get_settings
from fp2mp_core.failure import entry_failed
from fp2mp_core.redi.fusion import ReDIFusion
from fp2mp_core.state import (
    BlackBoard,
    ConfirmedFact,
    RawEntry,
    SubQuery,
    Task,
    WikiPage,
    board_message,
)
from fp2mp_core.wiki.index import build_index
from fp2mp_core.wiki.log import append_log_entry, make_log_entry
from fp2mp_core.wiki.maintenance import (
    compute_relevance_scores,
    merge_overlapping_pages,
    prune_wiki,
)
from fp2mp_core.wiki.page import _agent_slug, build_wiki_page, update_wiki_page, update_incoming_cross_refs

_SKIP_TYPES = {"init", "orchestrator_directive", "critique", "curator_summary"}

_s = get_settings()
_PROMOTE_THRESHOLD = _s.promote_threshold
_NORMATIVE_PROMOTE_THRESHOLD = _s.normative_promote_threshold
_EMPIRICAL_WEB_THRESHOLD = _s.empirical_web_promote_threshold
_MIN_CONTENT_LENGTH = _s.min_content_length

_fusion = ReDIFusion()


def _is_recommendation(entry: RawEntry, sub_query: SubQuery | None = None) -> bool:
    claim = entry.get("content", "")
    return (
        bool(entry.get("is_recommendation"))
        or (sub_query or {}).get("intent_aspect") == "recommendation"
        or claim.startswith(
            (
                "Рекомендуется",
                "Предлагается",
                "Рекомендация:",
                "Следует",
                "Recommend",
                "It is recommended",
            )
        )
    )


def _get_processable(raw_data: list[RawEntry], current_iteration: int) -> list[RawEntry]:
    """Return entries eligible for curation."""
    return [
        e for e in raw_data
        if e.get("type") not in _SKIP_TYPES
        and e.get("iteration", 0) <= current_iteration
    ]


def _entry_evidence_type(entry: RawEntry, state: BlackBoard) -> str:
    sq_id = entry.get("sub_query_id", "")
    return next(
        (
            sq.get("evidence_type", "factual")
            for sq in state.get("redi_decomposition", [])
            if sq.get("sub_query_id") == sq_id
        ),
        "factual",
    )


def _source_type(entry: RawEntry) -> str:
    agent_type = entry.get("type", "")
    if agent_type in {"urban_analysis", "code_result"}:
        return "computed"
    if agent_type == "web_findings":
        return "web"
    if agent_type == "normative_findings":
        return "normative"
    if agent_type == "mediator_synthesis":
        return "synthesis"
    return "web"


def _should_promote(entry: RawEntry, state: BlackBoard) -> bool:
    agent_type = entry.get("type", "")
    confidence = entry.get("confidence", 0.0)
    content = entry.get("content", "")
    ev_type = _entry_evidence_type(entry, state)

    if len(content) < _MIN_CONTENT_LENGTH:
        return False
    if agent_type == "normative_findings":
        return confidence >= _NORMATIVE_PROMOTE_THRESHOLD
    if agent_type in {"web_findings", "code_result", "mediator_synthesis", "urban_analysis"}:
        if ev_type == "empirical" and agent_type == "web_findings":
            return confidence >= _EMPIRICAL_WEB_THRESHOLD
        return confidence >= _PROMOTE_THRESHOLD
    return False


def _entry_to_fact(entry: RawEntry, sub_query: SubQuery | None = None) -> ConfirmedFact:
    fact_id = (
        f"fact_{entry.get('agent', 'x')}_{entry.get('sub_query_id', 'x')}_"
        f"{uuid.uuid4().hex[:6]}"
    )
    return ConfirmedFact(
        fact_id=fact_id,
        claim=entry.get("content", "")[:500],
        source_agents=[entry.get("agent", "unknown")],
        confidence=entry.get("confidence", 0.0),
        citations=entry.get("citations", []),
        limitations=[],
        sub_query_id=entry.get("sub_query_id", ""),
        is_recommendation=_is_recommendation(entry, sub_query),
        source_type=_source_type(entry),
    )


def wiki_curator_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for WikiCuratorAgent."""
    prev_iteration = state.get("iteration", 0)
    iteration = prev_iteration + 1  # increment here
    raw_data = state.get("raw_data", [])
    current_wiki = dict(state.get("wiki", {}))
    sub_queries: list[SubQuery] = state.get("redi_decomposition", [])
    sq_index = {sq["sub_query_id"]: sq for sq in sub_queries}

    # Process entries from this iteration batch (iteration-based, no mutation)
    processable = _get_processable(raw_data, prev_iteration)
    new_facts: list[ConfirmedFact] = []
    log_entries: list[str] = []

    # --- Build/update wiki pages from new entries ---
    for entry in processable:
        sq_id = entry.get("sub_query_id", "")
        sub_query = sq_index.get(sq_id)

        agent = entry.get("agent", "unknown")
        page_id = f"{_agent_slug(agent)}_{sq_id}" if sq_id else _agent_slug(agent)

        existing = current_wiki.get(page_id)
        if existing:
            new_page = update_wiki_page(existing, entry, current_wiki, iteration)
            action = "page_updated"
        else:
            new_page = build_wiki_page(entry, sub_query, current_wiki, iteration)
            action = "page_created"

        current_wiki[page_id] = new_page
        log_entries.append(
            make_log_entry(
                iteration=iteration,
                agent="WikiCurator",
                action=action,
                page_id=page_id,
                summary=(
                    f"conf={new_page.get('confidence', 0.0):.2f} | "
                    f"{entry.get('content', '')[:100]}"
                ),
            )
        )

        # Check for conflict markers
        if "> CONFLICT:" in new_page.get("content", ""):
            log_entries.append(
                make_log_entry(
                    iteration=iteration,
                    agent="WikiCurator",
                    action="conflict_flagged",
                    page_id=page_id,
                    summary="Contradiction detected between new and existing content.",
                )
            )

        # Promote to output if confidence threshold met
        if _should_promote(entry, state):
            fact = _entry_to_fact(entry, sub_query)
            new_facts.append(fact)
            log_entries.append(
                make_log_entry(
                    iteration=iteration,
                    agent="WikiCurator",
                    action="fact_promoted",
                    page_id=page_id,
                    summary=f"fact_id={fact['fact_id']} conf={fact['confidence']:.2f}",
                )
            )

    # --- Run ReDI Fusion ---
    if processable:
        fusion_page = _fusion.fuse(raw_data, sub_queries)
        fusion_page["updated_at_iteration"] = iteration
        current_wiki["redi_fusion"] = fusion_page

    # --- Wiki maintenance ---
    current_wiki = update_incoming_cross_refs(current_wiki)
    current_wiki = compute_relevance_scores(current_wiki, sub_queries)
    current_wiki = prune_wiki(current_wiki, sub_queries, iteration)
    current_wiki, merge_log = merge_overlapping_pages(current_wiki, iteration)
    for ml in merge_log:
        log_entries.append(
            make_log_entry(
                iteration=iteration,
                agent="WikiCurator",
                action="pages_merged",
                page_id="wiki",
                summary=ml,
            )
        )

    # --- Update log.md ---
    log_page = current_wiki.get("log.md")
    log_content = log_page.get("content", "# Change Log\n") if log_page else "# Change Log\n"
    for le in log_entries:
        log_content = append_log_entry(log_content, le)
    current_wiki["log.md"] = WikiPage(
        page_id="log.md",
        title="Change Log",
        content=log_content,
        updated_by="WikiCurator",
        updated_at_iteration=iteration,
        confidence=1.0,
        citations=[],
        tags=["system"],
        incoming_cross_refs=[],
        relevance_score=1.0,
    )

    # --- Update index.md ---
    current_wiki["index.md"] = WikiPage(
        page_id="index.md",
        title="Knowledge Index",
        content=build_index(current_wiki, iteration),
        updated_by="WikiCurator",
        updated_at_iteration=iteration,
        confidence=1.0,
        citations=[],
        tags=["system"],
        incoming_cross_refs=[],
        relevance_score=1.0,
    )

    # --- Quality-aware stagnation detection ---
    # A round that produced only failed / unusable entries is stagnation too,
    # even if entries were technically processed.
    useful = sum(
        1
        for e in processable
        if e.get("type") not in _SKIP_TYPES and not entry_failed(e)
    )
    progress_delta = len(processable)
    stagnation_count = state.get("stagnation_count", 0)
    if useful == 0:
        stagnation_count += 1
    else:
        stagnation_count = 0

    # --- Agent-proposed follow-up tasks (capped, deduped) ---
    followup_tasks: list[Task] = []
    existing_dirs = {t.get("directive", "") for t in state.get("tasks", [])}
    followup_cap = get_settings().max_followup_tasks_per_round
    for e in processable:
        if len(followup_tasks) >= followup_cap:
            break
        for s in e.get("follow_up_suggestions", []) or []:
            if len(followup_tasks) >= followup_cap:
                break
            directive = (s.get("directive", "") or "").strip()
            agent = s.get("assigned_agent", "")
            if not directive or directive in existing_dirs:
                continue
            existing_dirs.add(directive)
            followup_tasks.append(
                Task(
                    task_id=str(uuid.uuid4()),
                    sub_query_id=e.get("sub_query_id", ""),
                    assigned_agent=agent,
                    status="pending",
                    priority="medium",
                    proposer="agent_followup",
                    created_at_iteration=iteration,
                    directive=directive,
                )
            )

    logger.info(
        "iter=%d curator | curated=%d new_facts=%d wiki_pages=%d stagnation=%d",
        iteration, len(processable), len(new_facts), len(current_wiki), stagnation_count,
    )
    curator_msg = board_message(
        agent="WikiCurator",
        iteration=iteration,
        msg_type="curator_summary",
        content=(
            f"Curated {len(processable)} entries. "
            f"Wiki pages: {len(current_wiki)}. "
            f"New facts: {len(new_facts)}. "
            f"Stagnation: {stagnation_count}."
        ),
        confidence=1.0,
    )
    curator_msg["curated"] = True

    return {
        "wiki": current_wiki,
        "output": new_facts,
        "raw_data": [curator_msg],
        "tasks": followup_tasks,
        "iteration": iteration,
        "stagnation_count": stagnation_count,
        "progress_delta": progress_delta,
        "current_stage": "curated",
        "agent_trace": [
            {
                "node": "wiki_curator",
                "iteration": iteration,
                "curated": len(processable),
                "new_facts": len(new_facts),
                "wiki_pages": len(current_wiki),
            }
        ],
    }


def route_from_curator(state: BlackBoard) -> str:
    """Decide next node after WikiCurator."""
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 6)

    if iteration >= max_iterations:
        return "finish_ready"

    output = state.get("output", [])
    if output:
        # After the first real iteration, generate hypotheses before critiquing
        if iteration == 1:
            return "hypothesis"
        return "critic"

    return "continue"
