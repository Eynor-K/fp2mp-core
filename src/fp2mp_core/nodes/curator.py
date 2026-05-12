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

import uuid
from typing import Any

from fp2mp_core.redi.fusion import ReDIFusion
from fp2mp_core.state import (
    BlackBoard,
    ConfirmedFact,
    RawEntry,
    SubQuery,
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
from fp2mp_core.wiki.page import WikiPageBuilder, update_incoming_cross_refs

# Confidence thresholds for promoting facts to output
_PROMOTE_THRESHOLD = 0.65
_NORMATIVE_PROMOTE_THRESHOLD = 0.7
_MIN_CONTENT_LENGTH = 50
_SKIP_TYPES = {"init", "orchestrator_directive", "critique", "curator_summary"}

_builder = WikiPageBuilder()
_fusion = ReDIFusion()


def _get_processable(raw_data: list[RawEntry], current_iteration: int) -> list[RawEntry]:
    """Return entries eligible for curation: not system messages, from current or prior iterations."""
    return [
        e for e in raw_data
        if e.get("type") not in _SKIP_TYPES
        and e.get("iteration", 0) <= current_iteration
    ]


def _should_promote(entry: RawEntry) -> bool:
    agent_type = entry.get("type", "")
    confidence = entry.get("confidence", 0.0)
    content = entry.get("content", "")

    if len(content) < _MIN_CONTENT_LENGTH:
        return False
    if agent_type == "normative_findings":
        return confidence >= _NORMATIVE_PROMOTE_THRESHOLD
    if agent_type in {"web_findings", "code_result", "mediator_synthesis"}:
        return confidence >= _PROMOTE_THRESHOLD
    return False


def _entry_to_fact(entry: RawEntry) -> ConfirmedFact:
    return ConfirmedFact(
        fact_id=f"fact_{entry.get('agent', 'x')}_{entry.get('sub_query_id', 'x')}_{uuid.uuid4().hex[:6]}",
        claim=entry.get("content", "")[:500],
        source_agents=[entry.get("agent", "unknown")],
        confidence=entry.get("confidence", 0.0),
        citations=entry.get("citations", []),
        limitations=[],
        sub_query_id=entry.get("sub_query_id", ""),
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

    from fp2mp_core.wiki.page import _agent_slug

    # --- Build/update wiki pages from new entries ---
    for entry in processable:
        sq_id = entry.get("sub_query_id", "")
        sub_query = sq_index.get(sq_id)

        agent = entry.get("agent", "unknown")
        page_id = f"{_agent_slug(agent)}_{sq_id}" if sq_id else _agent_slug(agent)

        existing = current_wiki.get(page_id)
        if existing:
            new_page = _builder.update_page(existing, entry, current_wiki, iteration)
            action = "page_updated"
        else:
            new_page = _builder.build(entry, sub_query, current_wiki, iteration)
            action = "page_created"

        current_wiki[page_id] = new_page
        log_entries.append(
            make_log_entry(
                iteration=iteration,
                agent="WikiCurator",
                action=action,
                page_id=page_id,
                summary=f"conf={new_page.get('confidence', 0.0):.2f} | {entry.get('content', '')[:100]}",
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
        if _should_promote(entry):
            fact = _entry_to_fact(entry)
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

    # --- Stagnation detection ---
    progress_delta = len(processable)
    stagnation_count = state.get("stagnation_count", 0)
    if progress_delta == 0:
        stagnation_count += 1
    else:
        stagnation_count = 0

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
        return "critic"

    return "continue"
