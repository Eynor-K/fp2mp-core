"""
FinalSynthesis node — converts confirmed facts and wiki into a structured markdown answer.

Called after the loop exits (CriticAgent decided STOP or iteration limit reached).
Optionally persists wiki to disk if WIKI_PERSIST_DIR is configured.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

logger = logging.getLogger("fp2mp_core.synthesis")

from fp2mp_core.config import DATA_DIR, get_settings
from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard


_COMMON_RULES = """\

ALWAYS, regardless of question type (mandatory, universal):
- PRESERVE THE COMMITTED ANSWER. You are formatting an answer that has already
  committed to a specific conclusion. Keep that exact commitment and its
  concrete figures, names, identifiers and values. Never dilute a specific
  result into a vague generality, and never turn it back into criteria.
- FAIL OPEN: never refuse, never reply that evidence is insufficient, never
  replace the answer with "open questions". If evidence is thin, keep the
  committed best answer and lower the stated confidence instead.
- Add a **Sources & Citations** section listing documents / URLs / computed
  outputs behind the answer. If there are none, write exactly:
  "No verifiable sources were available."
- End with ONE short **Confidence & caveats** line (low/medium/high + the single
  most important caveat). This is a one-liner, not a deferral section.
"""


def _get_synthesis_system(question_intent: str) -> str:
    base = """\
You are a technical editor. A committed, concrete answer to the question has
ALREADY been drafted (provided as "Committed draft"). Your job is to FORMAT and
tighten it — never weaken its commitment, never add hedging or open-question
sections, never reintroduce a refusal. Domain-neutral: no domain template.

You also receive confirmed facts, contradictions and an evidence-status line —
use them only to add citations and sanity-check specifics, not to soften the
answer.

"""
    if question_intent in {"planning", "analytical"}:
        return base + """\
Produce clean Markdown:
1. **Answer** — the committed answer up front (specific option/value/decision),
   2-4 sentences.
2. **Rationale & Key Evidence** — why, grounded in facts (computed > normative > web).
3. **Regulatory Constraints** (only if applicable).
4. **Geometry Files** — if geometry file paths are provided, list them briefly.
""" + _COMMON_RULES
    if question_intent == "regulatory":
        return base + """\
Produce clean Markdown:
1. **Answer** — the committed direct answer citing specific documents.
2. **Applicable Regulations** — documents with sections.
3. **Key Requirements** — specific constraints and rules.
""" + _COMMON_RULES
    return base + """\
Produce clean Markdown:
1. **Answer** — the committed direct answer (2-4 sentences, specific).
2. **Key Findings** — bulleted, with [source].
3. **Quantitative Analysis** (only if applicable).
""" + _COMMON_RULES


def final_synthesis_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node — produces the final answer."""
    question = state.get("question", "")
    output_facts = state.get("output", [])
    wiki = state.get("wiki", {})
    critique = state.get("critique", {})
    question_intent = state.get("question_intent", "factual")

    # Build facts string
    facts_str = ""
    for f in sorted(output_facts, key=lambda x: x.get("confidence", 0), reverse=True):
        sq = f.get("sub_query_id", "?")
        conf = f.get("confidence", 0.0)
        agents = ", ".join(f.get("source_agents", []))
        source_type = f.get("source_type", "unknown")
        claim = f.get("claim", "")
        limitations = "; ".join(f.get("limitations", []))
        cit_urls = [c.get("url", c.get("document", "")) for c in f.get("citations", [])]
        cit_str = " | ".join(filter(None, cit_urls))
        facts_str += (
            f"\n- [{sq} conf={conf:.2f} type={source_type} src={agents}] {claim}"
            + (f"\n  Limitations: {limitations}" if limitations else "")
            + (f"\n  Sources: {cit_str}" if cit_str else "")
        )

    synthesis_page = wiki.get("synthesis", {})
    synthesis_text = synthesis_page.get("content", "") if synthesis_page else ""

    contradictions = critique.get("contradictions", [])
    contradictions_str = "\n".join(f"- {c}" for c in contradictions) if contradictions else "None"

    overall_conf = critique.get("overall_confidence", 0.0)
    iteration = state.get("iteration", 0)

    draft_answer = state.get("draft_answer", "")

    # Evidence-status signal — fail-open: thin evidence lowers confidence, it
    # never turns into a refusal.
    if not output_facts:
        evidence_status = (
            "Few/no facts were promoted. Keep the committed draft answer; set "
            "confidence to low and add one short caveat. Do NOT refuse."
        )
    else:
        by_type: dict[str, int] = {}
        for f in output_facts:
            by_type[f.get("source_type", "unknown")] = by_type.get(f.get("source_type", "unknown"), 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
        evidence_status = (
            f"{len(output_facts)} confirmed facts ({breakdown}). "
            "Ground every claim in these; do not generalize beyond them."
        )

    # Detect geometry files produced by agents
    geometry_str = ""
    outputs_dir = DATA_DIR / "outputs"
    if outputs_dir.exists():
        geo_files = sorted(outputs_dir.glob("*.geojson")) + sorted(outputs_dir.glob("*.csv"))
        if geo_files:
            geometry_str = "\n".join(
                f"- {p.name} ({p.stat().st_size // 1024} KB): {p}"
                for p in geo_files
            )

    prompt = f"""\
Question: {question}
Question intent: {question_intent}

Committed draft (FORMAT THIS — preserve its commitment and specifics):
{draft_answer or "(no draft provided — commit to the best weighted answer from the facts below)"}

Confirmed facts (for citations / specifics):
{facts_str or "No confirmed facts — keep the committed draft, mark confidence low."}

Mediator synthesis:
{synthesis_text[:1200] if synthesis_text else "Not available."}

Unresolved contradictions:
{contradictions_str}

Evidence status: {evidence_status}

Overall confidence: {overall_conf:.2f}
Iterations used: {iteration}
{f"Geometry files (saved by agents):{chr(10)}{geometry_str}" if geometry_str else ""}

Format the committed answer now. Do not weaken or defer it.
"""

    try:
        llm = get_chat_model(temperature=0.1)
        response = llm.invoke([
            {"role": "system", "content": _get_synthesis_system(question_intent)},
            {"role": "user", "content": prompt},
        ])
        final_answer = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        # Fail-open fallback: keep the committed draft if we have one.
        if draft_answer:
            final_answer = draft_answer
        else:
            final_answer = _fallback_answer(question, output_facts, contradictions, overall_conf)
        final_answer += f"\n\n*Note: LLM formatting failed: {exc}*"

    new_output_facts = []
    if not _has_recommendation_fact(output_facts):
        recommendation_claim = _extract_recommendation_claim(final_answer)
        if recommendation_claim:
            rec_sub_query_id = next(
                (
                    sq["sub_query_id"]
                    for sq in state.get("redi_decomposition", [])
                    if sq.get("intent_aspect") == "recommendation"
                ),
                "sq_rec",
            )
            new_output_facts.append(
                {
                    "fact_id": f"fact_final_recommendation_{uuid.uuid4().hex[:8]}",
                    "claim": recommendation_claim[:500],
                    "source_agents": ["FinalSynthesis"],
                    "confidence": overall_conf or _average_confidence(output_facts),
                    "citations": [],
                    "limitations": [
                        "Derived during final synthesis from available confirmed facts."
                    ],
                    "sub_query_id": rec_sub_query_id,
                    "is_recommendation": True,
                    "source_type": "synthesis",
                }
            )

    # Optionally persist wiki
    settings = get_settings()
    if settings.wiki_persist_dir:
        try:
            from fp2mp_core.tools.wiki_io import persist_wiki
            persist_wiki(wiki, settings.wiki_persist_dir)
        except Exception:
            pass

    logger.info(
        "synthesis done | intent=%s facts=%d geometry_files=%s",
        question_intent,
        len(output_facts),
        bool(geometry_str),
    )
    return {
        "final_answer": final_answer,
        "output": new_output_facts,
        "current_stage": "finished",
        "stop_flag": True,
    }


def _has_recommendation_fact(facts: list) -> bool:
    prefixes = (
        "Рекомендуется",
        "Предлагается",
        "Рекомендация:",
        "Следует",
        "Recommend",
        "It is recommended",
    )
    return any(
        f.get("is_recommendation") or f.get("claim", "").startswith(prefixes)
        for f in facts
    )


def _extract_recommendation_claim(final_answer: str) -> str:
    match = re.search(
        r"^##\s+(?:Recommendations|Рекомендации)\s*$([\s\S]*?)(?=^##\s+|\Z)",
        final_answer,
        re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return ""

    for line in match.group(1).splitlines():
        cleaned = line.strip().lstrip("-*").strip()
        cleaned = re.sub(r"^\d+[.)]\s*", "", cleaned)
        if cleaned:
            return cleaned
    return ""


def _average_confidence(facts: list) -> float:
    if not facts:
        return 0.5
    return sum(f.get("confidence", 0.0) for f in facts) / len(facts)


def _fallback_answer(
    question: str,
    facts: list,
    contradictions: list[str],
    overall_conf: float,
) -> str:
    lines = [
        f"# Answer to: {question}\n",
        f"*Overall confidence: {overall_conf:.2f}*\n",
        "## Key Findings\n",
    ]
    for f in facts:
        claim = f.get("claim", "")
        conf = f.get("confidence", 0.0)
        agents = ", ".join(f.get("source_agents", []))
        lines.append(f"- [{conf:.2f}] {claim} *(sources: {agents})*")

    if contradictions:
        lines.append("\n## Unresolved Contradictions\n")
        for c in contradictions:
            lines.append(f"- {c}")

    return "\n".join(lines)
