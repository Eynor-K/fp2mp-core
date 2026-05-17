"""
OrchestratorAgent — reads BlackBoard state and decides which agents to activate.

Key behaviors:
- Routes every sub-query through capability-aware LLM selection
- Anti-duplicate: never re-assigns a (agent, sub_query_id) pair already done
- Reassigns failed attempts to agents that have not tried the sub-query yet
- Anti-stagnation: routes to Critic if stagnation_count >= 2
- Max 3 dispatches per round to avoid context explosion
- Picks up new_tasks injected by CriticAgent
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langgraph.types import Send

from fp2mp_core.capabilities import AGENT_CAPABILITIES, AgentCapability
from fp2mp_core.llm import get_chat_model
from fp2mp_core.nodes.blackboard import wiki_briefing
from fp2mp_core.state import BlackBoard, OrchestratorDirective, RawEntry, Task, board_message

_MAX_DISPATCHES_PER_ROUND = 3

_AGENT_NODES = {
    "WebSearchAgent": "web_search_agent",
    "NormativeAgent": "normative_agent",
    "CodeSpatialAgent": "code_spatial_agent",
    "BlocksNetAgent": "blocksnet_agent",
    "MediatorAgent": "mediator",
}

_SPECIALIZED_AGENTS = set(AGENT_CAPABILITIES.keys())


def _format_agent_capabilities(capabilities: dict[str, AgentCapability]) -> str:
    sections: list[str] = []
    for capability in capabilities.values():
        sections.append(
            "\n".join(
                [
                    f"[{capability.name}]",
                    f"  Description: {capability.description}",
                    f"  Handles: {'; '.join(capability.handles)}",
                    f"  Cannot do: {'; '.join(capability.cannot_do)}",
                ]
            )
        )
    return "\n\n".join(sections)


_ROUTING_SYSTEM = f"""\
You are the OrchestratorAgent for an open-ended QA system.

Your role: read the BlackBoard and decide which agents to dispatch next.

Available agents:
- MediatorAgent: synthesis when multiple agent results exist but need cross-source analysis

Specialized agent capabilities:
{_format_agent_capabilities(AGENT_CAPABILITIES)}

Routing rule:
- ReDI search_modality is a decomposer hint, not a binding constraint.
- Choose the specialized agent whose capabilities best fit the sub-query.

Spatial routing rule:
- If the sub-query asks WHICH streets/buildings/zones/routes EXIST in a named city area,
  or asks to COUNT, MEASURE, or CLASSIFY geographic features → choose CodeSpatialAgent.
  These questions are answered by OpenStreetMap via osmnx, not by web search.
- Only use WebSearchAgent for such questions if CodeSpatialAgent has already failed.
- If the sub-query requires BlocksNet urban indicators (accessibility, provision, density,
  centrality, diversity) and city data has been loaded into data/ → choose BlocksNetAgent.
  It operates on pre-loaded city data and is faster and more accurate than CodeSpatialAgent
  for these metrics.

Rules:
1. Assign based on agent capabilities, using search_modality only as a hint.
2. Choose the agent most likely to fill the coverage gap.
3. Never re-dispatch an agent for a sub_query_id that already has status="done".
4. Dispatch MediatorAgent only when at least 2 other agents have produced results.
5. Limit to 3 dispatches per round.
6. If stagnation_count >= 2 or all tasks done → action="critic".

When asked to choose one agent, output only the requested JSON object.
"""


def _get_done_pairs(tasks: list[Task]) -> set[tuple[str, str]]:
    """Set of (agent, sub_query_id) pairs with status done."""
    return {
        (t.get("assigned_agent", ""), t.get("sub_query_id", ""))
        for t in tasks
        if t.get("status") == "done"
    }


def _raw_entry_failed(entry: RawEntry) -> bool:
    content = entry.get("content", "").strip()
    return entry.get("confidence", 1.0) < 0.4 or content.startswith("Agent stopped due to")


def _tried_agents_for_sub_query(
    tasks: list[Task], raw_data: list[RawEntry], sq_id: str
) -> set[str]:
    tried: set[str] = set()
    for task in tasks:
        if task.get("sub_query_id") == sq_id and task.get("status") in {"done", "in_progress"}:
            agent = task.get("assigned_agent", "")
            if agent in _SPECIALIZED_AGENTS:
                tried.add(agent)
    for entry in raw_data:
        if entry.get("sub_query_id") == sq_id:
            agent = entry.get("agent", "")
            if agent in _SPECIALIZED_AGENTS:
                tried.add(agent)
    return tried


def _detect_and_reassign_failed(state: BlackBoard) -> list[Task]:
    tasks: list[Task] = state.get("tasks", [])
    raw_data: list[RawEntry] = state.get("raw_data", [])
    iteration = state.get("iteration", 0)
    new_tasks: list[Task] = []

    pending_sq_ids = {t.get("sub_query_id", "") for t in tasks if t.get("status") == "pending"}
    reassigned_sq_ids: set[str] = set()

    for task in tasks:
        sq_id = task.get("sub_query_id", "")
        assigned_agent = task.get("assigned_agent", "")
        if not sq_id or assigned_agent not in _SPECIALIZED_AGENTS:
            continue
        if task.get("status") != "done":
            continue
        if sq_id in pending_sq_ids or sq_id in reassigned_sq_ids:
            continue

        matching_entries = [
            entry
            for entry in raw_data
            if entry.get("sub_query_id") == sq_id and entry.get("agent") == assigned_agent
        ]
        if not matching_entries or not any(_raw_entry_failed(entry) for entry in matching_entries):
            continue

        has_success = any(
            entry.get("sub_query_id") == sq_id
            and entry.get("agent") in _SPECIALIZED_AGENTS
            and not _raw_entry_failed(entry)
            for entry in raw_data
        )
        if has_success:
            continue

        tried_agents = _tried_agents_for_sub_query(tasks, raw_data, sq_id)
        if tried_agents >= _SPECIALIZED_AGENTS:
            continue

        new_tasks.append(
            Task(
                task_id=str(uuid.uuid4()),
                sub_query_id=sq_id,
                status="pending",
                priority=task.get("priority", "medium"),
                proposer="OrchestratorAgent",
                created_at_iteration=iteration,
                directive=task.get("directive", ""),
            )
        )
        reassigned_sq_ids.add(sq_id)

    return new_tasks


def _count_results_by_type(raw_data: list[RawEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in raw_data:
        t = entry.get("type", "other")
        counts[t] = counts.get(t, 0) + 1
    return counts


def orchestrator_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for OrchestratorAgent."""
    iteration = state.get("iteration", 0)
    stagnation = state.get("stagnation_count", 0)
    tasks: list[Task] = state.get("tasks", [])
    sub_queries = state.get("redi_decomposition", [])
    raw_data = state.get("raw_data", [])

    reassigned_tasks = _detect_and_reassign_failed(state)
    all_tasks = tasks + reassigned_tasks

    # --- Force to critic on stagnation ---
    if stagnation >= 2:
        return _route_to_critic(state, iteration, reason="stagnation")

    # --- Get pending tasks ---
    pending_tasks = [t for t in all_tasks if t.get("status") == "pending"]
    if not pending_tasks:
        # All tasks done — trigger Mediator if enough results, else go to Critic
        result_counts = _count_results_by_type(raw_data)
        agent_results = (
            result_counts.get("web_findings", 0)
            + result_counts.get("normative_findings", 0)
            + result_counts.get("code_result", 0)
        )
        if agent_results >= 2 and "mediator_synthesis" not in result_counts:
            return _dispatch_mediator(state, iteration)
        return _route_to_critic(state, iteration, reason="all_tasks_done")

    done_pairs = _get_done_pairs(all_tasks)
    sub_query_index = {sq["sub_query_id"]: sq for sq in sub_queries}
    assigned_agents_by_task_id: dict[str, str] = {}

    # --- Determine dispatches ---
    dispatches: list[OrchestratorDirective] = []

    for task in pending_tasks:
        if len(dispatches) >= _MAX_DISPATCHES_PER_ROUND:
            break

        sq_id = task.get("sub_query_id", "")
        sq = sub_query_index.get(sq_id, {})
        tried_agents = _tried_agents_for_sub_query(all_tasks, raw_data, sq_id)
        agent = _llm_choose_agent(sq, state, exclude_agents=tried_agents)
        pair = (agent, sq_id)

        if pair in done_pairs:
            continue

        modality = sq.get("search_modality", "web")
        assigned_agents_by_task_id[task.get("task_id", "")] = agent

        # Build directive text
        sq_text = sq.get("text", task.get("directive", ""))
        enriched_variants = sq.get("enriched_variants", [])
        directive_text = sq_text
        if enriched_variants:
            directive_text += f"\n\nSearch hints: {'; '.join(enriched_variants[:2])}"

        domain_hints = sq.get("domain_hints", [])
        if domain_hints and agent == "NormativeAgent":
            directive_text += f"\n\nFocus on: {', '.join(domain_hints)}"

        dispatches.append(
            OrchestratorDirective(
                directive_id=str(uuid.uuid4()),
                target_agent=agent,
                task_id=task.get("task_id", ""),
                sub_query_id=sq_id,
                question=directive_text,
                rationale=f"Assigned by capability-aware routing; modality hint was '{modality}'",
                priority=task.get("priority", "medium"),
            )
        )

    if not dispatches:
        return _route_to_critic(state, iteration, reason="no_valid_dispatches")

    # Mark dispatched tasks as in_progress
    task_ids_dispatched = {d.get("task_id", "") for d in dispatches}
    updated_tasks = []
    for t in all_tasks:
        if t.get("task_id") in task_ids_dispatched:
            t = dict(t)  # type: ignore[assignment]
            t["status"] = "in_progress"
            t["assigned_agent"] = assigned_agents_by_task_id.get(
                t.get("task_id", ""), t.get("assigned_agent", "")
            )
        updated_tasks.append(t)

    target_agents = [d.get("target_agent") for d in dispatches]
    orch_msg = board_message(
        agent="OrchestratorAgent",
        iteration=iteration,
        msg_type="orchestrator_directive",
        content=f"Dispatching {len(dispatches)} agents: {target_agents}",
        confidence=1.0,
    )
    orch_msg["curated"] = True

    return {
        "orchestrator_directives": dispatches,
        "tasks": updated_tasks,
        "raw_data": [orch_msg],
        "next_action": "dispatch",
        "current_stage": "dispatching",
        "agent_trace": [
            {
                "node": "orchestrator",
                "iteration": iteration,
                "dispatches": [d.get("target_agent") for d in dispatches],
            }
        ],
    }


def _route_to_critic(state: BlackBoard, iteration: int, reason: str) -> dict[str, Any]:
    msg = board_message(
        agent="OrchestratorAgent",
        iteration=iteration,
        msg_type="orchestrator_directive",
        content=f"Routing to critic. Reason: {reason}",
        confidence=1.0,
    )
    msg["curated"] = True
    return {
        "raw_data": [msg],
        "next_action": "critic",
        "current_stage": "routing_to_critic",
        "orchestrator_directives": [],
    }


def _dispatch_mediator(state: BlackBoard, iteration: int) -> dict[str, Any]:
    msg = board_message(
        agent="OrchestratorAgent",
        iteration=iteration,
        msg_type="orchestrator_directive",
        content="Dispatching MediatorAgent for cross-source synthesis.",
        confidence=1.0,
    )
    msg["curated"] = True
    return {
        "raw_data": [msg],
        "orchestrator_directives": [
            OrchestratorDirective(
                directive_id=str(uuid.uuid4()),
                target_agent="MediatorAgent",
                task_id="",
                sub_query_id="",
                question="Synthesize all agent findings into a coherent answer.",
                rationale="All search tasks completed; synthesis needed.",
                priority="high",
            )
        ],
        "next_action": "dispatch",
        "current_stage": "dispatching_mediator",
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found")
    return json.loads(stripped[start:end + 1])


def _llm_choose_agent(sq: dict, state: BlackBoard, exclude_agents: set[str] | None = None) -> str:
    """Use LLM to choose the best specialized agent from capability cards."""
    excluded = exclude_agents or set()
    brief = wiki_briefing(state, limit=800)
    available_agents = {
        name: capability
        for name, capability in AGENT_CAPABILITIES.items()
        if name not in excluded
    }
    if not available_agents:
        return "WebSearchAgent"

    prompt = f"""\
{_ROUTING_SYSTEM}

Task: {sq.get('text', '')}
Intent: {sq.get('intent_aspect', '')}
Initial modality hint from decomposer: {sq.get('search_modality', 'any')}

Current wiki state:
{brief[:800]}

Available agents for this task:
{_format_agent_capabilities(available_agents)}

Excluded agents: {', '.join(sorted(excluded)) if excluded else 'none'}

Choose the agent best suited for the task.
Consider the modality hint, but if another agent is clearly better, choose that agent.
Do not choose an excluded agent.

Reply strictly as JSON:
{{"agent": "<agent name>", "rationale": "<one sentence>"}}
"""
    try:
        llm = get_chat_model(temperature=0.0)
        response = llm.invoke(prompt)
        text = (response.content if hasattr(response, "content") else str(response)).strip()
        parsed = _parse_json_object(text)
        agent = str(parsed.get("agent", ""))
        if agent in available_agents:
            return agent
    except Exception:
        pass
    if "WebSearchAgent" in available_agents:
        return "WebSearchAgent"  # safe default
    return next(iter(available_agents))


def route_from_orchestrator(state: BlackBoard):
    """
    Conditional edge: returns a list of Send() for parallel agent dispatch,
    or a string for direct routing to critic/mediator.
    """
    next_action = state.get("next_action", "dispatch")
    directives = state.get("orchestrator_directives", [])

    if next_action == "critic" or not directives:
        return "critic"

    # Build Send list for parallel execution
    sends = []
    for directive in directives[:_MAX_DISPATCHES_PER_ROUND]:
        target = directive.get("target_agent", "WebSearchAgent")
        node = _AGENT_NODES.get(target)
        if node:
            sends.append(Send(node, dict(state)))

    if not sends:
        return "critic"

    return sends
