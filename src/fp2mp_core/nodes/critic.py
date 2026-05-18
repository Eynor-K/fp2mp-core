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
import logging
import uuid
from typing import Any

logger = logging.getLogger("fp2mp_core.critic")

from fp2mp_core.llm import call_with_thinking
from fp2mp_core.nodes.context import coverage_from_sub_queries, wiki_briefing
from fp2mp_core.state import BlackBoard, CritiqueResult, Task, board_message

_SYSTEM = """\
You are the CriticAgent. Your sole purpose is to evaluate answer quality and decide
whether to STOP (sufficient evidence) or CONTINUE (more research needed).

Inputs you receive:
- Original question
- ReDI sub-queries and their coverage status
- Confirmed output facts with confidence scores
- Wiki synthesis summary
- Current iteration and stagnation count

HOLISTIC JUDGEMENT (most important):
Do NOT decide based only on per-sub-query coverage. Judge whether the confirmed
evidence, taken together, ACTUALLY answers the ORIGINAL question. Set
"question_answered"=true only if a reader would consider the original question
genuinely answered by the evidence — not merely that each sub-query has some fact.

STOP only if "question_answered" is true. Otherwise CONTINUE and specify which
sub-queries still need work and what kind of research is missing.

Additional CONTINUE triggers (even if sub-queries look covered):
- No integrative answer/recommendation has been derived for the original question.
- For planning/analytical/spatial questions: no confirmed fact comes from a
  quantitative agent (BlocksNetAgent/CodeSpatialAgent, source_type="computed").
(Iteration limit / stagnation are handled externally via force_stop.)

Output format (JSON only):
{
  "action": "STOP" or "CONTINUE",
  "question_answered": true or false,
  "overall_confidence": 0.0-1.0,
  "reasoning": "<concise explanation grounded in the original question>",
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
    else:
        critique = _run_thinking_critique(state, iteration)

    stop_flag = critique.get("force_stop", False) or critique.get("action") == "STOP"
    logger.info(
        "iter=%d critic | action=%s conf=%.2f force=%s new_tasks=%d",
        iteration,
        critique.get("action", "?"),
        critique.get("overall_confidence", 0.0),
        critique.get("force_stop", False),
        len(critique.get("new_tasks", [])),
    )

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


def _run_thinking_critique(state: BlackBoard, iteration: int) -> CritiqueResult:
    coverage = coverage_from_sub_queries(state)
    output = state.get("output", [])
    sub_queries = state.get("redi_decomposition", [])

    # Quick check: if all covered, we may not even need LLM
    all_covered = all(v == "covered" for v in coverage.values()) if coverage else False
    question_intent = state.get("question_intent", "factual")
    needs_quant = question_intent in {"planning", "analytical", "spatial"}
    has_computed = any(
        f.get("source_type") == "computed"
        or set(f.get("source_agents", [])) & {"BlocksNetAgent", "CodeSpatialAgent"}
        for f in output
    )
    has_recommendation = any(
        f.get("is_recommendation")
        or any(
            f.get("claim", "").startswith(kw)
            for kw in (
                "Рекомендуется",
                "Предлагается",
                "Рекомендация:",
                "Следует",
                "Recommend",
                "It is recommended",
            )
        )
        for f in output
    )
    if all_covered and output and needs_quant and not has_computed:
        max_iterations = state.get("max_iterations", 6) or 6
        if iteration < max_iterations - 1:
            overall = sum(f.get("confidence", 0.0) for f in output) / max(len(output), 1)
            empirical_sqs = [
                sq for sq in sub_queries
                if sq.get("evidence_type") == "empirical"
            ]
            new_tasks = [
                {
                    "sub_query_id": sq["sub_query_id"],
                    "assigned_agent": "BlocksNetAgent",
                    "directive": sq.get("text", "") + "\nProvide quantitative data.",
                    "priority": "high",
                }
                for sq in empirical_sqs[:2]
            ]
            if not new_tasks:
                new_tasks = [
                    {
                        "sub_query_id": "sq_empirical",
                        "assigned_agent": "BlocksNetAgent",
                        "directive": (
                            "Provide quantitative evidence for the planning/spatial question: "
                            + state.get("question", "")
                        ),
                        "priority": "high",
                    }
                ]
            return CritiqueResult(
                action="CONTINUE",
                coverage=coverage,
                overall_confidence=overall,
                reasoning="Question needs quantitative evidence but no computed facts exist yet.",
                new_tasks=new_tasks,
                contradictions=[],
                force_stop=False,
            )

    # Holistic: covered sub-queries alone are NOT enough — only stop early when
    # an integrative answer/recommendation for the original question exists.
    if all_covered and output and has_recommendation and not (needs_quant and not has_computed):
        overall = sum(f.get("confidence", 0.0) for f in output) / max(len(output), 1)
        return CritiqueResult(
            action="STOP",
            coverage=coverage,
            overall_confidence=overall,
            reasoning="All sub-queries covered and an integrative answer for the original question is present.",
            new_tasks=[],
            contradictions=[],
            force_stop=False,
        )
    if all_covered and output and not has_recommendation and iteration >= 2:
        overall = sum(f.get("confidence", 0.0) for f in output) / max(len(output), 1)
        rec_sub_query_id = next(
            (
                sq["sub_query_id"]
                for sq in sub_queries
                if sq.get("intent_aspect") == "recommendation"
            ),
            "sq_rec",
        )
        return CritiqueResult(
            action="CONTINUE",
            coverage=coverage,
            overall_confidence=overall,
            reasoning="All sub-queries covered but no recommendation derived yet.",
            new_tasks=[
                {
                    "sub_query_id": rec_sub_query_id,
                    "assigned_agent": "WebSearchAgent",
                    "directive": (
                        "Synthesize all available findings and COMMIT to the single "
                        "best-supported concrete answer/decision (name the specific "
                        "option/value, not just criteria) that directly answers: "
                        + state.get("question", "")
                    ),
                    "priority": "high",
                }
            ],
            contradictions=[],
            force_stop=False,
        )

    sq_summary = "\n".join(
        f"- {sq_id}: {status}" for sq_id, status in coverage.items()
    )
    facts_summary = "\n".join(
        f"- [{f.get('sub_query_id','?')} conf={f.get('confidence',0):.2f}] "
        f"{f.get('claim','')[:200]}"
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

        # Holistic gate: only override a STOP if the model EXPLICITLY says the
        # original question is not yet answered. A STOP without the field is
        # respected (avoids non-termination on older/looser critic outputs).
        if action == "STOP" and data.get("question_answered") is False:
            action = "CONTINUE"
            reasoning = (reasoning + " | Original question not yet fully answered.").strip()

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
