"""
HypothesisNode — generates competing hypotheses after initial data collection.

Runs after the first curator iteration (iteration >= 1) when there are confirmed
facts. Produces 2-3 testable hypotheses for verification by agents in subsequent
iterations, preventing the system from treating early web results as final answers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fp2mp_core.llm import get_chat_model
from fp2mp_core.nodes.context import wiki_briefing
from fp2mp_core.state import BlackBoard, Task, board_message

_SYSTEM = """\
You are a hypothesis generator for an open-ended analytical system.

Given a question and initial evidence, generate 2-3 specific, competing hypotheses
that could answer the question. Each hypothesis must:
1. Be specific and falsifiable — not vague statements
2. Be verifiable by data analysis, spatial computation, or targeted search
3. Identify exactly what data or computation would confirm or refute it
4. Prefer quantitative verification (BlocksNetAgent, CodeSpatialAgent) over more web searches

Output JSON only:
{
  "hypotheses": [
    {
      "hypothesis_id": "h1",
      "claim": "<specific, falsifiable claim>",
      "evidence_needed": "<what data or computation would confirm this>",
      "verification_agent": "BlocksNetAgent|CodeSpatialAgent|WebSearchAgent|NormativeAgent",
      "verification_task": "<specific instruction for the chosen agent>",
      "sub_query_id": "<matching sub_query_id from the list, or empty string>"
    }
  ]
}
"""


def hypothesis_node(state: BlackBoard) -> dict[str, Any]:
    """Generate competing hypotheses from initial facts for agent verification."""
    question = state.get("question", "")
    output = state.get("output", [])
    iteration = state.get("iteration", 0)
    sub_queries = state.get("redi_decomposition", [])

    if not output:
        return {"agent_trace": [{"node": "hypothesis", "skipped": True, "reason": "no_facts_yet"}]}

    brief = wiki_briefing(state, limit=1500)
    facts_summary = "\n".join(
        f"- [{f.get('sub_query_id', '?')} conf={f.get('confidence', 0):.2f} "
        f"type={f.get('source_type', '')}] {f.get('claim', '')[:200]}"
        for f in output[:12]
    )
    sq_texts = "\n".join(
        f"- {sq['sub_query_id']} [{sq.get('evidence_type', 'factual')}]: {sq.get('text', '')}"
        for sq in sub_queries
    )

    prompt = f"""\
Question: {question}

Sub-queries:
{sq_texts}

Initial confirmed facts:
{facts_summary}

Current knowledge summary:
{brief[:800]}

Generate 2-3 specific, competing hypotheses that could answer the main question.
Prefer hypotheses that can be verified with quantitative urban data (BlocksNetAgent)
or spatial computation (CodeSpatialAgent) rather than more web searches.
Use the sub_query_id of the most relevant existing sub-query for each hypothesis.
"""

    try:
        llm = get_chat_model(temperature=0.3)
        response = llm.invoke([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ])
        text = (response.content if hasattr(response, "content") else str(response)).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        text = text.rstrip("`").strip()
        data = json.loads(text)
        hypotheses = data.get("hypotheses", [])
    except Exception:
        return {"agent_trace": [{"node": "hypothesis", "skipped": True, "reason": "parse_error"}]}

    if not hypotheses:
        return {"agent_trace": [{"node": "hypothesis", "skipped": True, "reason": "empty_result"}]}

    sq_ids = {sq["sub_query_id"] for sq in sub_queries}
    new_tasks: list[Task] = []
    for h in hypotheses:
        agent = h.get("verification_agent", "WebSearchAgent")
        sq_id = h.get("sub_query_id", "") or _find_empirical_sq(sub_queries)
        if sq_id not in sq_ids:
            sq_id = _find_empirical_sq(sub_queries)
        new_tasks.append(Task(
            task_id=str(uuid.uuid4()),
            sub_query_id=sq_id,
            assigned_agent=agent,
            status="pending",
            priority="high",
            proposer="HypothesisNode",
            created_at_iteration=iteration,
            directive=h.get("verification_task", h.get("claim", "")),
        ))

    hyp_msg = board_message(
        agent="HypothesisNode",
        iteration=iteration,
        msg_type="orchestrator_directive",
        content=(
            f"Generated {len(hypotheses)} hypotheses at iteration {iteration}:\n"
            + "\n".join(
                f"- {h.get('hypothesis_id', '?')}: {h.get('claim', '')[:150]}"
                for h in hypotheses
            )
        ),
        confidence=1.0,
    )
    hyp_msg["curated"] = True

    return {
        "tasks": new_tasks,
        "raw_data": [hyp_msg],
        "agent_trace": [{"node": "hypothesis", "iteration": iteration, "hypotheses": len(hypotheses)}],
    }


def _find_empirical_sq(sub_queries: list) -> str:
    for sq in sub_queries:
        if sq.get("evidence_type") == "empirical":
            return sq["sub_query_id"]
    return sub_queries[0]["sub_query_id"] if sub_queries else "sq_001"
