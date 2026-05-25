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
import re
import uuid
from typing import Any

from langgraph.types import Send

from fp2mp_core.capabilities import AGENT_CAPABILITIES, AgentCapability
from fp2mp_core.config import get_model_locality
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

_URBAN_METRIC_RE = re.compile(
    r"обеспеч|доступн|связност|плотност|разнообраз|центральност|сервис|населен|населени|"
    r"квартал|землепольз|освоенн|застрой|coverage|accessibility|density|diversity|centrality|service|block",
    re.IGNORECASE,
)
_DEVELOPMENT_PROPOSAL_RE = re.compile(
    r"предлож|гипотез|развити|постро|размест|измен|смен|оптимиз|мастер-план|"
    r"proposal|hypothesis|develop|build|place|optimi[sz]e|land.?use",
    re.IGNORECASE,
)


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

BlocksNet routing rule:
- For questions about service provision/population coverage, walking or transport accessibility,
  service centrality/diversity/density, urban form/development indicators, and land-use
  accessibility for the city that already exists in the local data/model model → choose
  BlocksNetAgent. It uses precomputed BlocksNet analytics and is more reliable than ad-hoc
  code for these urban metrics.
- Use CodeSpatialAgent, not BlocksNetAgent, when the question is about an arbitrary place outside
  the local model or requires fresh geodata from OSM/the web.
- If the question asks for development proposals, what/where to build, land-use change,
  or service-mix optimization for the local model city, BlocksNetAgent can generate
  measured hypotheses through local model tools.

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


def _model_locality_matches(text: str) -> bool:
    try:
        locality = get_model_locality()
    except Exception:
        return False
    if not locality:
        return False
    lowered = text.lower()
    names = {str(locality.get("name", "")).lower()}
    names.update(part.strip().lower() for part in str(locality.get("display_name", "")).split(","))
    for name in names:
        if not name:
            continue
        if name in lowered:
            return True
        if len(name) >= 6 and name[:5] in lowered:
            return True
    return False


def _blocksnet_metric_directives(base_text: str, slots_left: int, context_text: str = "") -> list[str]:
    if slots_left <= 1:
        return [base_text]
    match_text = f"{base_text}\n{context_text}"
    if not _model_locality_matches(match_text) or not _URBAN_METRIC_RE.search(base_text):
        return [base_text]

    if _DEVELOPMENT_PROPOSAL_RE.search(match_text):
        return [
            f"{base_text}\n\n"
            "Вопрос просит предложения/гипотезы развития. Сначала используй измеренные метрики модели "
            "для выбора зоны; если нужны конкретные варианты 'что и где построить', вызови "
            "suggest_target_blocks и затем, только если это уместно по задаче, propose_zone_development. "
            "Верни HYPOTHESES и FOLLOW_UPS для нормативной/веб-проверки, не трактуй TPE как обязательное решение."
        ]

    directives = [
        (
            f"{base_text}\n\n"
            "Сфокусируйся только на обеспеченности ключевыми сервисами в нормативное время. "
            "Сначала вызови list_key_services(), выбери 1-2 релевантных сервиса и их accessibility, "
            "затем вызови compute_service_provision для каждого выбранного сервиса. "
            "В ответе интерпретируй полную/частичную/нулевую обеспеченность и назови проблемные block_id."
        ),
        (
            f"{base_text}\n\n"
            "Сфокусируйся только на транспортной доступности и связности кварталов. "
            "Вызови compute_mean_accessibility, compute_median_accessibility и compute_connectivity. "
            "Сравни лучшие и худшие кварталы по block_id и дай численную интерпретацию."
        ),
        (
            f"{base_text}\n\n"
            "Сфокусируйся только на сервисном разнообразии, центральности и плотности/освоенности. "
            "Вызови compute_shannon_diversity, compute_services_centrality и compute_density_indicators. "
            "Свяжи метрики с выводами для мастер-плана, цитируя конкретные числа и block_id."
        ),
    ]
    return directives[:slots_left]


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
    pending_tasks = sorted(
        [t for t in all_tasks if t.get("status") == "pending"],
        key=lambda t: (0 if t.get("sub_query_id") == "sq_code_support" else 1),
    )
    if not pending_tasks:
        # All tasks done — trigger Mediator if enough results, else go to Critic
        result_counts = _count_results_by_type(raw_data)
        agent_results = (
            result_counts.get("web_findings", 0)
            + result_counts.get("normative_findings", 0)
            + result_counts.get("code_result", 0)
            + result_counts.get("blocksnet_result", 0)
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
        if sq.get("search_modality") == "code":
            code_agents = {"CodeSpatialAgent", "BlocksNetAgent"}
            excluded = tried_agents | (_SPECIALIZED_AGENTS - code_agents)
            agent = _llm_choose_agent(sq, state, exclude_agents=excluded)
        else:
            agent = _llm_choose_agent(sq, state, exclude_agents=tried_agents)
        if task.get("assigned_agent") in _SPECIALIZED_AGENTS and task.get("assigned_agent") not in tried_agents:
            agent = task.get("assigned_agent", agent)

        pair = (agent, sq_id)
        if pair in done_pairs:
            continue

        modality = sq.get("search_modality", "web")
        assigned_agents_by_task_id[task.get("task_id", "")] = agent

        # Build directive text
        sq_text = sq.get("text", "")
        enriched_variants = sq.get("enriched_variants", [])
        directive_text = task.get("directive") or sq_text
        if enriched_variants:
            directive_text += f"\n\nSearch hints: {'; '.join(enriched_variants[:2])}"

        domain_hints = sq.get("domain_hints", [])
        if domain_hints and agent == "NormativeAgent":
            directive_text += f"\n\nFocus on: {', '.join(domain_hints)}"

        directive_variants = [directive_text]
        if agent == "BlocksNetAgent":
            directive_variants = _blocksnet_metric_directives(
                directive_text,
                _MAX_DISPATCHES_PER_ROUND - len(dispatches),
                state.get("question", ""),
            )

        for variant in directive_variants:
            if len(dispatches) >= _MAX_DISPATCHES_PER_ROUND:
                break
            dispatches.append(OrchestratorDirective(
                directive_id=str(uuid.uuid4()),
                target_agent=agent,
                task_id=task.get("task_id", ""),
                sub_query_id=sq_id,
                question=variant,
                rationale=f"Assigned by capability-aware routing; modality hint was '{modality}'",
                priority=task.get("priority", "medium"),
            ))

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

    model_context = ""
    try:
        locality = get_model_locality()
    except Exception:
        locality = None
    if locality:
        model_context = (
            "\nLocal model context:\n"
            f"Локальная модель data/model покрывает локацию: {locality['display_name']}.\n"
            "Если вопрос касается этой локации и просит метрики обеспеченности/"
            "доступности/плотности/разнообразия/центральности или предложения "
            "развития/оптимизации набора сервисов — предпочитай "
            "BlocksNetAgent. Если вопрос про другое место или нужны свежие данные "
            "OSM/веба — выбирай CodeSpatialAgent. Сопоставляй названия с учётом "
            "языка и транслитерации.\n"
        )

    prompt = f"""\
{_ROUTING_SYSTEM}

Task: {sq.get('text', '')}
Intent: {sq.get('intent_aspect', '')}
Initial modality hint from decomposer: {sq.get('search_modality', 'any')}
{model_context}

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

    # Build one Send per target agent. Agent nodes process all directives for
    # their target; sending once per directive would duplicate expensive work.
    directives_by_node: dict[str, list[OrchestratorDirective]] = {}
    for directive in directives[:_MAX_DISPATCHES_PER_ROUND]:
        target = directive.get("target_agent", "WebSearchAgent")
        node = _AGENT_NODES.get(target)
        if node:
            directives_by_node.setdefault(node, []).append(directive)

    sends = []
    for node, node_directives in directives_by_node.items():
        node_state = dict(state)
        node_state["orchestrator_directives"] = node_directives
        sends.append(Send(node, node_state))

    if not sends:
        return "critic"

    return sends
