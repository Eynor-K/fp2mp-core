"""
MediatorAgent — synthesis of cross-source knowledge.

Reads the full wiki and output facts, finds agreements and contradictions,
writes a "synthesis" wiki page, and creates new ConfirmedFacts.
Always routes to wiki_curator after completion.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fp2mp_core.llm import call_with_thinking
from fp2mp_core.state import BlackBoard, Citation, ConfirmedFact, WikiPage, board_message

_SYSTEM = """\
You are the MediatorAgent. Your role is to synthesize knowledge from multiple agents
into a coherent, well-structured understanding of the question.

You have access to:
- All wiki pages produced by specialist agents (web, normative, code)
- Confirmed facts already promoted to output

Your tasks:
1. AGREEMENTS: identify where multiple agents corroborate the same claim
2. CONTRADICTIONS: identify where agents disagree; flag with confidence difference
3. SYNTHESIS: write a coherent narrative for each sub-query aspect
4. UPDATE OUTPUT: produce new ConfirmedFacts with multi-source attribution
5. RECOMMENDATION: based on the synthesized evidence, derive at least one actionable
   recommendation that directly answers the original question.
   - If evidence is conclusive, state the recommendation directly.
   - If evidence is partial, provide a conditional recommendation:
     "If [condition], then [action] is recommended because [rationale]."
   - Mark provisional recommendations explicitly.
   Do NOT skip this step even if the data is incomplete.

Rules:
- Do NOT suppress contradictions — flag them explicitly in the synthesis page
- Only promote a fact to output if at least 2 independent sources agree OR confidence >= 0.75
- Attribute each claim to its source agents

Output format (JSON only):
{
  "synthesis_narrative": "<markdown text for synthesis wiki page>",
  "new_facts": [
    {
      "claim": "<factual statement>",
      "source_agents": ["AgentA", "AgentB"],
      "confidence": 0.0,
      "sub_query_id": "sq_001",
      "is_recommendation": true,
      "limitations": ["limitation1"],
      "citations": [{"url": "...", "document": "..."}]
    }
  ],
  "contradictions": ["<description of contradiction 1>", ...]
}
"""


def mediator_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for MediatorAgent."""
    iteration = state.get("iteration", 0)
    wiki = state.get("wiki", {})
    output_facts = state.get("output", [])
    sub_queries = state.get("redi_decomposition", [])

    # Build wiki summary for mediator (exclude index/log overhead)
    wiki_content_parts = []
    for page_id, page in wiki.items():
        if page_id in {"index.md", "log.md"}:
            continue
        title = page.get("title", page_id)
        content = page.get("content", "")[:800]
        conf = page.get("confidence", 0.0)
        wiki_content_parts.append(f"### [{page_id}] {title} (conf={conf:.2f})\n{content}")
    wiki_summary = "\n\n".join(wiki_content_parts[:12])  # cap context

    facts_summary = "\n".join(
        f"- [{f.get('sub_query_id','?')} conf={f.get('confidence',0):.2f}] "
        f"{f.get('claim','')[:200]}"
        for f in output_facts[:20]
    )

    sq_summary = "\n".join(
        f"- {sq['sub_query_id']}: {sq.get('text', '')}" for sq in sub_queries
    )

    prompt = f"""\
Original question sub-queries:
{sq_summary}

Wiki pages:
{wiki_summary}

Currently confirmed facts:
{facts_summary or "None yet."}

Please synthesize the above into a coherent understanding, identify agreements and contradictions,
and produce new confirmed facts where multiple sources agree.
"""

    try:
        _thinking, answer_text = call_with_thinking(
            prompt=prompt,
            system=_SYSTEM,
            budget_tokens=4000,
            max_tokens=6000,
        )

        # Parse JSON response
        text = answer_text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        text = text.rstrip("`").strip()

        data = json.loads(text)
        synthesis_narrative = data.get("synthesis_narrative", answer_text)
        raw_facts = data.get("new_facts", [])
        contradictions = data.get("contradictions", [])

    except (json.JSONDecodeError, Exception) as exc:
        synthesis_narrative = f"Mediator synthesis (parse error: {exc})\n\n{answer_text[:1000]}"
        raw_facts = []
        contradictions = []

    # Build ConfirmedFacts
    new_facts: list[ConfirmedFact] = []
    for rf in raw_facts:
        raw_cits = rf.get("citations", [])
        citations: list[Citation] = [Citation(**c) for c in raw_cits if isinstance(c, dict)]
        fact = ConfirmedFact(
            fact_id=f"fact_mediator_{uuid.uuid4().hex[:8]}",
            claim=rf.get("claim", "")[:500],
            source_agents=rf.get("source_agents", ["MediatorAgent"]),
            confidence=float(rf.get("confidence", 0.5)),
            citations=citations,
            limitations=rf.get("limitations", []),
            sub_query_id=rf.get("sub_query_id", ""),
            is_recommendation=bool(rf.get("is_recommendation", False)),
            source_type="synthesis",
        )
        new_facts.append(fact)

    # Build synthesis wiki page
    contradiction_section = ""
    if contradictions:
        contradiction_section = "\n\n## Unresolved Contradictions\n" + "\n".join(
            f"- {c}" for c in contradictions
        )

    synthesis_page = WikiPage(
        page_id="synthesis",
        title="Mediator Synthesis",
        content=synthesis_narrative + contradiction_section,
        updated_by="MediatorAgent",
        updated_at_iteration=iteration,
        confidence=0.8,
        citations=[],
        tags=["synthesis", "mediator"],
        incoming_cross_refs=[],
        relevance_score=1.5,
    )

    mediator_msg = board_message(
        agent="MediatorAgent",
        iteration=iteration,
        msg_type="mediator_synthesis",
        content=synthesis_narrative[:400],
        confidence=0.8,
    )

    return {
        "wiki": {"synthesis": synthesis_page},
        "output": new_facts,
        "raw_data": [mediator_msg],
        "current_stage": "mediated",
        "agent_trace": [
            {
                "node": "mediator",
                "iteration": iteration,
                "new_facts": len(new_facts),
                "contradictions": len(contradictions),
            }
        ],
    }
