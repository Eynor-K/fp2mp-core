"""
BlackBoard state — the single source of truth shared by all agents.

Three-tier knowledge structure:
  raw_data  → wiki (LLM-Wiki pages)  → output (confirmed facts)
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# Leaf data types
# ---------------------------------------------------------------------------


class Citation(TypedDict, total=False):
    title: str
    url: str
    quote: str
    document: str
    section: str


class SubQuery(TypedDict, total=False):
    sub_query_id: str        # "sq_001", "sq_002", …
    text: str
    intent_aspect: str       # e.g. "empirical data", "regulatory constraint"
    search_modality: str     # "web" | "normative" | "code" | "any"
    independence: bool       # can be answered independently of other sub-queries?
    enriched_variants: list[str]
    keywords: list[str]
    domain_hints: list[str]  # e.g. ["ГОСТ", "СНиП"] for normative agent


class Task(TypedDict, total=False):
    task_id: str
    sub_query_id: str
    assigned_agent: str      # "WebSearchAgent" | "NormativeAgent" | "CodeSpatialAgent"
    status: str              # "pending" | "in_progress" | "done" | "failed"
    priority: str            # "high" | "medium" | "low"
    proposer: str
    created_at_iteration: int
    directive: str           # natural-language instruction for the agent


class RawEntry(TypedDict, total=False):
    entry_id: str
    agent: str
    iteration: int
    type: str                # "web_findings" | "normative_findings" | "code_result"
                             # | "mediator_synthesis" | "orchestrator_directive"
    content: str
    confidence: float
    tool_trace: list[dict[str, Any]]
    citations: list[Citation]
    sub_query_id: str
    curated: bool            # True once WikiCurator has processed this entry


class WikiPage(TypedDict, total=False):
    page_id: str             # slug, e.g. "web_search_sq001"
    title: str
    content: str             # markdown with [[cross-refs]] and > CONFLICT: markers
    updated_by: str
    updated_at_iteration: int
    confidence: float
    citations: list[Citation]
    tags: list[str]
    incoming_cross_refs: list[str]   # page_ids that reference this page
    relevance_score: float   # = len(incoming_cross_refs) × confidence × sq_match_bonus


class ConfirmedFact(TypedDict, total=False):
    fact_id: str
    claim: str
    source_agents: list[str]
    confidence: float
    citations: list[Citation]
    limitations: list[str]
    sub_query_id: str


class CritiqueResult(TypedDict, total=False):
    action: str              # "CONTINUE" | "STOP"
    coverage: dict[str, str] # sub_query_id → "covered" | "partial" | "pending"
    overall_confidence: float
    reasoning: str
    new_tasks: list[dict[str, Any]]
    contradictions: list[str]
    force_stop: bool


class OrchestratorDirective(TypedDict, total=False):
    directive_id: str
    target_agent: str
    task_id: str
    sub_query_id: str
    question: str
    rationale: str
    priority: str


class LogEntry(TypedDict):
    timestamp: str
    iteration: int
    agent: str
    action: str              # "page_created" | "page_updated" | "fact_promoted"
                             # | "conflict_flagged" | "pages_merged" | "page_pruned"
    page_id: str
    summary: str


# ---------------------------------------------------------------------------
# Reducer helpers
# ---------------------------------------------------------------------------


def _merge_by_key(left: list, right: list, key: str) -> list:
    """Upsert right into left by key field — right wins on conflict."""
    if not left:
        return list(right or [])
    if not right:
        return list(left)
    index: dict[str, dict] = {item[key]: item for item in left if key in item}
    for item in right:
        if key in item:
            index[item[key]] = item
    return list(index.values())


def _merge_tasks(left: list[Task], right: list[Task]) -> list[Task]:
    return _merge_by_key(left, right, "task_id")  # type: ignore[return-value]


def _merge_wiki(left: dict[str, WikiPage], right: dict[str, WikiPage]) -> dict[str, WikiPage]:
    """Dict merge — right wins; preserves left keys not in right."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _merge_output(left: list[ConfirmedFact], right: list[ConfirmedFact]) -> list[ConfirmedFact]:
    """Upsert by fact_id; keep the entry with higher confidence."""
    if not left:
        return list(right or [])
    if not right:
        return list(left)
    index: dict[str, ConfirmedFact] = {f["fact_id"]: f for f in left if "fact_id" in f}
    for fact in right:
        fid = fact.get("fact_id", "")
        if not fid:
            continue
        existing = index.get(fid)
        if existing is None or fact.get("confidence", 0) >= existing.get("confidence", 0):
            index[fid] = fact
    return list(index.values())


# ---------------------------------------------------------------------------
# Main BlackBoard state
# ---------------------------------------------------------------------------


class BlackBoard(TypedDict, total=False):
    # Input
    question: str

    # ReDI decomposition
    redi_decomposition: list[SubQuery]

    # Task queue (upsert by task_id)
    tasks: Annotated[list[Task], _merge_tasks]

    # Tier 1: raw agent outputs (append-only)
    raw_data: Annotated[list[RawEntry], operator.add]

    # Tier 2: structured LLM-Wiki (upsert by page_id)
    wiki: Annotated[dict[str, WikiPage], _merge_wiki]

    # Tier 3: confirmed facts for final answer
    output: Annotated[list[ConfirmedFact], _merge_output]

    # Loop control
    iteration: int
    max_iterations: int
    stop_flag: bool
    critique: CritiqueResult
    final_answer: str | None

    # Orchestration
    orchestrator_directives: list[OrchestratorDirective]
    next_action: str         # "dispatch" | "critic" | "finish"
    current_stage: str

    # Monitoring
    agent_trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    stagnation_count: int
    progress_delta: int      # new raw_data entries added this round (stagnation detection)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def create_initial_state(question: str, max_iterations: int = 6) -> BlackBoard:
    return BlackBoard(
        question=question,
        redi_decomposition=[],
        tasks=[],
        raw_data=[],
        wiki={},
        output=[],
        iteration=0,
        max_iterations=max_iterations,
        stop_flag=False,
        critique={},
        final_answer=None,
        orchestrator_directives=[],
        next_action="dispatch",
        current_stage="init",
        agent_trace=[],
        errors=[],
        stagnation_count=0,
        progress_delta=0,
    )


def board_message(
    agent: str,
    iteration: int,
    msg_type: str,
    content: str,
    sub_query_id: str = "",
    confidence: float = 0.0,
    tool_trace: list[dict[str, Any]] | None = None,
    citations: list[Citation] | None = None,
) -> RawEntry:
    """Factory for RawEntry — the standard way agents write to raw_data."""
    return RawEntry(
        entry_id=str(uuid.uuid4()),
        agent=agent,
        iteration=iteration,
        type=msg_type,
        content=content,
        confidence=confidence,
        tool_trace=tool_trace or [],
        citations=citations or [],
        sub_query_id=sub_query_id,
        curated=False,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
