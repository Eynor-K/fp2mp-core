"""
CriticAgent — STOP/CONTINUE decision.

Force-stop conditions (no LLM needed):
- iteration >= max_iterations
- stagnation_count >= 2

Quality-based STOP: all sub-queries have confirmed facts with confidence >= 0.7.
CONTINUE: emits new_tasks with specific gaps for Orchestrator.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from fp2mp_core.llm import call_with_thinking
from fp2mp_core.nodes.blackboard import coverage_from_sub_queries, wiki_briefing
from fp2mp_core.state import BlackBoard, CritiqueResult, Task, board_message
from fp2mp_core.summarize import compress_text

_SYSTEM = """\
You are the CriticAgent. Your sole purpose is to evaluate answer quality and decide
whether to STOP (sufficient evidence) or CONTINUE (more research needed).

Inputs you receive:
- Original question
- ReDI sub-queries and their coverage status
- Confirmed output facts with confidence scores
- Wiki synthesis summary
- Current iteration and stagnation count

STOP criteria (ANY of):
1. All sub-queries have at least one confirmed fact with confidence >= 0.7
2. The confirmed facts together provide a complete, well-supported answer
3. Iteration limit or stagnation (handled externally — check force_stop field)

CONTINUE: specify which sub-queries still need more work and what kind of research is missing.

Output format (JSON only):
{
  "action": "STOP" or "CONTINUE",
  "overall_confidence": 0.0-1.0,
  "reasoning": "<concise explanation>",
  "new_tasks": [
    {
      "sub_query_id": "sq_001",
      "assigned_agent": "WebSearchAgent|NormativeAgent|CodeSpatialAgent|BlocksNetAgent",
      "directive": "<specific instruction>",
      "priority": "high|medium|low"
    }
  ],
  "contradictions": ["<unresolved contradiction 1>", ...]
}

new_tasks must be empty if action is STOP.
"""


def _build_force_stop_critique(state: BlackBoard, reason: str) -> CritiqueResult:
    coverage = coverage_from_sub_queries(state)
    overall = 0.0
    output = state.get("output", [])
    if output:
        overall = sum(f.get("confidence", 0.0) for f in output) / len(output)

    return CritiqueResult(
        action="STOP",
        coverage=coverage,
        overall_confidence=overall,
        reasoning=f"Force stop: {reason}",
        new_tasks=[],
        contradictions=[],
        force_stop=True,
    )


def critic_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for CriticAgent."""
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 6)
    stagnation = state.get("stagnation_count", 0)

    # --- Force stop (no LLM) ---
    if iteration >= max_iterations:
        critique = _build_force_stop_critique(state, f"max_iterations={max_iterations} reached")
    elif stagnation >= 2:
        critique = _build_force_stop_critique(state, f"stagnation_count={stagnation} >= 2")
    elif _code_support_pending(state):
        critique = CritiqueResult(
            action="CONTINUE",
            coverage=coverage_from_sub_queries(state),
            overall_confidence=0.3,
            reasoning="Quantitative code support has not run yet; continue before stopping.",
            new_tasks=[],
            contradictions=[],
            force_stop=False,
        )
    else:
        critique = _run_thinking_critique(state, iteration)

    if not critique.get("force_stop", False):
        metric_followups = _blocksnet_metric_followups(state, iteration)
        if metric_followups:
            critique["action"] = "CONTINUE"
            critique["new_tasks"] = list(critique.get("new_tasks", [])) + metric_followups
            critique["reasoning"] = (
                str(critique.get("reasoning", ""))
                + " BlocksNet result was too thin for the urban metric task; requesting focused metrics."
            ).strip()
        else:
            hypothesis_followups = _blocksnet_hypothesis_followups(state)
            if hypothesis_followups and critique.get("action") == "CONTINUE":
                critique["new_tasks"] = list(critique.get("new_tasks", [])) + hypothesis_followups
                critique["reasoning"] = (
                    str(critique.get("reasoning", ""))
                    + " BlocksNet hypotheses exposed follow-up validation tasks."
                ).strip()

    stop_flag = critique.get("force_stop", False) or critique.get("action") == "STOP"

    # Inject new tasks into BlackBoard if CONTINUE
    new_tasks: list[Task] = []
    for t in critique.get("new_tasks", []):
        new_tasks.append(
            Task(
                task_id=str(uuid.uuid4()),
                sub_query_id=t.get("sub_query_id", ""),
                assigned_agent=t.get("assigned_agent", "WebSearchAgent"),
                status="pending",
                priority=t.get("priority", "medium"),
                proposer="CriticAgent",
                created_at_iteration=iteration,
                directive=t.get("directive", ""),
            )
        )

    critic_msg = board_message(
        agent="CriticAgent",
        iteration=iteration,
        msg_type="critique",
        content=(
            f"Action: {critique.get('action')} | "
            f"Confidence: {critique.get('overall_confidence', 0):.2f} | "
            f"{critique.get('reasoning', '')[:200]}"
        ),
        confidence=critique.get("overall_confidence", 0.0),
    )
    critic_msg["curated"] = True

    return {
        "critique": critique,
        "stop_flag": stop_flag,
        "tasks": new_tasks,
        "next_action": "finish" if stop_flag else "continue",
        "raw_data": [critic_msg],
        "current_stage": "critique",
        "agent_trace": [
            {
                "node": "critic",
                "iteration": iteration,
                "action": critique.get("action"),
                "overall_confidence": critique.get("overall_confidence", 0.0),
                "force_stop": critique.get("force_stop", False),
            }
        ],
    }


def _code_support_pending(state: BlackBoard) -> bool:
    sub_queries = state.get("redi_decomposition", [])
    if not any(sq.get("sub_query_id") == "sq_code_support" for sq in sub_queries):
        return False
    return not any(
        entry.get("agent") in {"CodeSpatialAgent", "BlocksNetAgent"}
        and entry.get("sub_query_id") == "sq_code_support"
        for entry in state.get("raw_data", [])
    )


def _blocksnet_metric_followups(state: BlackBoard, iteration: int) -> list[dict[str, Any]]:
    tasks = state.get("tasks", [])
    sub_queries = state.get("redi_decomposition", [])
    raw_data = state.get("raw_data", [])
    existing = {
        (task.get("sub_query_id", ""), task.get("assigned_agent", ""), task.get("proposer", ""))
        for task in tasks
    }
    followups: list[dict[str, Any]] = []
    for sq in sub_queries:
        sq_id = sq.get("sub_query_id", "")
        text = sq.get("text", "")
        if not sq_id or not _looks_like_urban_metric(text):
            continue
        if (sq_id, "BlocksNetAgent", "CriticAgent") in existing:
            continue
        entries = [
            entry for entry in raw_data
            if entry.get("agent") == "BlocksNetAgent" and entry.get("sub_query_id") == sq_id
        ]
        if not entries:
            continue
        if any(not _thin_blocksnet_entry(entry) for entry in entries):
            continue
        followups.append({
            "sub_query_id": sq_id,
            "assigned_agent": "BlocksNetAgent",
            "directive": (
                f"Доделай количественную проверку для под-вопроса: {text}\n"
                "Не ограничивайся обзором данных. Сформулируй ANALYSIS PLAN, сам выбери необходимые "
                "семейства измерений из контекста и проверь выводы/гипотезы числами и block_id. "
                "Верни RESULT, HYPOTHESES с verdict и REFLECTION."
            ),
            "priority": "high",
        })
    return followups


def _blocksnet_hypothesis_followups(state: BlackBoard) -> list[dict[str, Any]]:
    tasks = state.get("tasks", [])
    existing = {
        (task.get("sub_query_id", ""), task.get("assigned_agent", ""), task.get("directive", ""))
        for task in tasks
    }
    followups: list[dict[str, Any]] = []
    for entry in state.get("raw_data", []):
        if entry.get("agent") != "BlocksNetAgent" or entry.get("type") != "blocksnet_result":
            continue
        content = entry.get("content", "")
        section = _section(content, "FOLLOW_UPS")
        lines = _followup_directive_chunks(section) if section else []
        hypotheses = _section(content, "HYPOTHESES")
        if hypotheses and "unverified" not in hypotheses.lower() and "verdict" in hypotheses.lower():
            lines.append(
                "NormativeAgent: проверить нормативную допустимость и ограничения для проверенных BlocksNet-гипотез развития: "
                + compress_text(hypotheses, max_chars=500, focus="preserve verdicts, exact numbers, and block_id values")
            )
            if _looks_like_development_hypothesis(hypotheses) and "compute_scenario_provision" not in content:
                lines.append(
                    "BlocksNetAgent: независимо проверить гипотезу развития через compute_scenario_provision, "
                    "сохранив before/after provision, block_id и verdict."
                )
        if not lines:
            continue
        for line in lines:
            directive = line.strip().lstrip("-*").strip()
            if not directive:
                continue
            agent = _followup_agent(directive)
            if not agent:
                continue
            directive = _strip_agent_prefix(directive)
            key = (entry.get("sub_query_id", ""), agent, directive)
            if key in existing:
                continue
            followups.append({
                "sub_query_id": entry.get("sub_query_id", ""),
                "assigned_agent": agent,
                "directive": directive,
                "priority": "medium",
            })
            existing.add(key)
    return followups[:3]


def _section(text: str, name: str) -> str:
    names = ("ANALYSIS PLAN", "RESULT", "REFLECTION", "HYPOTHESES", "NUMERIC SELF-CHECK", "FOLLOW_UPS", "CONFIDENCE", "LIMITATIONS")
    pattern = re.compile(rf"^({'|'.join(re.escape(item) for item in names)}):\s*(.*)$", re.MULTILINE)
    matches = list(pattern.finditer(text or ""))
    for index, match in enumerate(matches):
        if match.group(1) != name:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return (match.group(2) + text[start:end]).strip()
    return ""


def _followup_agent(text: str) -> str:
    for agent in ("WebSearchAgent", "NormativeAgent", "CodeSpatialAgent", "BlocksNetAgent"):
        if text.startswith(f"{agent}:"):
            return agent
    return ""


def _followup_directive_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    for line in str(text or "").splitlines():
        parts = re.split(
            r"\s*;\s*(?=(?:WebSearchAgent|NormativeAgent|CodeSpatialAgent|BlocksNetAgent):)",
            line,
        )
        chunks.extend(part for part in parts if part.strip())
    return chunks


def _looks_like_development_hypothesis(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "развит", "землепольз", "сервис", "tpe", "оптимиз", "стро",
            "development", "land-use", "optimizer", "build",
        )
    )


def _strip_agent_prefix(text: str) -> str:
    for agent in ("WebSearchAgent", "NormativeAgent", "CodeSpatialAgent", "BlocksNetAgent"):
        prefix = f"{agent}:"
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _looks_like_urban_metric(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "обеспеч", "доступн", "связност", "плотност", "разнообраз", "центральност",
            "сервис", "квартал", "землепольз", "освоенн", "accessibility", "coverage",
            "density", "diversity", "centrality", "service", "block",
        )
    )


def _thin_blocksnet_entry(entry: dict[str, Any]) -> bool:
    if entry.get("confidence", 0.0) < 0.65:
        return True
    content = entry.get("content", "")
    if "REFLECTION:" not in content:
        return True
    tools: list[str] = []
    for trace in entry.get("tool_trace", []):
        for step in trace.get("intermediate_steps", []):
            tools.append(str(step.get("tool", "")))
    compute_tools = [tool for tool in tools if tool.startswith("compute_")]
    return len(set(compute_tools)) == 0


def _run_thinking_critique(state: BlackBoard, iteration: int) -> CritiqueResult:
    coverage = coverage_from_sub_queries(state)
    output = state.get("output", [])
    sub_queries = state.get("redi_decomposition", [])

    # Quick check: if all covered, we may not even need LLM
    all_covered = all(v == "covered" for v in coverage.values()) if coverage else False
    if all_covered and output:
        overall = sum(f.get("confidence", 0.0) for f in output) / max(len(output), 1)
        return CritiqueResult(
            action="STOP",
            coverage=coverage,
            overall_confidence=overall,
            reasoning="All sub-queries have confirmed facts with confidence >= 0.7.",
            new_tasks=[],
            contradictions=[],
            force_stop=False,
        )

    sq_summary = "\n".join(
        f"- {sq_id}: {status}" for sq_id, status in coverage.items()
    )
    facts_summary = "\n".join(
        f"- [{f.get('sub_query_id','?')} conf={f.get('confidence',0):.2f}] "
        f"{compress_text(f.get('claim',''), max_chars=400, focus='preserve exact numbers, IDs, hypotheses, and gaps')}"
        for f in output[:15]
    )
    sq_texts = "\n".join(
        f"- {sq['sub_query_id']}: {sq.get('text', '')}" for sq in sub_queries
    )

    brief = wiki_briefing(state, limit=1500)

    prompt = f"""\
Original question: {state.get('question', '')}

Sub-queries:
{sq_texts}

Coverage status:
{sq_summary}

Confirmed facts:
{facts_summary or "None yet."}

Wiki summary:
{brief}

Iteration: {iteration} / {state.get('max_iterations', 6)}
Stagnation count: {state.get('stagnation_count', 0)}

Evaluate whether the current evidence is sufficient to answer the original question.
"""

    try:
        _thinking, answer_text = call_with_thinking(
            prompt=prompt,
            system=_SYSTEM,
            budget_tokens=5000,
            max_tokens=8000,
        )

        text = answer_text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        text = text.rstrip("`").strip()

        data = json.loads(text)
        action = data.get("action", "CONTINUE")
        overall_conf = float(data.get("overall_confidence", 0.5))
        reasoning = data.get("reasoning", "")
        new_tasks_raw = data.get("new_tasks", [])
        contradictions = data.get("contradictions", [])

        # Override coverage with computed values (more reliable than LLM)
        return CritiqueResult(
            action=action,
            coverage=coverage,
            overall_confidence=overall_conf,
            reasoning=reasoning,
            new_tasks=new_tasks_raw,
            contradictions=contradictions,
            force_stop=False,
        )

    except Exception as exc:
        # On parse failure: conservative CONTINUE
        return CritiqueResult(
            action="CONTINUE",
            coverage=coverage,
            overall_confidence=0.3,
            reasoning=f"Critique parse error: {exc}. Defaulting to CONTINUE.",
            new_tasks=[],
            contradictions=[],
            force_stop=False,
        )


def route_from_critic(state: BlackBoard) -> str:
    """Routing function for conditional edge after critic."""
    if state.get("stop_flag", False):
        return "finish"
    critique = state.get("critique", {})
    if critique.get("action") == "STOP" or critique.get("force_stop", False):
        return "finish"
    return "continue"
