"""
answer_commit_node — final Mediator-style drafter (gemini pattern).

Runs once the loop is finished (critic STOP / max iterations / curator
finish_ready). It reads the curated knowledge and ALWAYS commits to a
concrete, weighted answer to the original question. It never refuses, never
substitutes the answer with a list of criteria or open questions. Confidence
and caveats are expressed inline. Domain-neutral by design.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("fp2mp_core.answer")

from fp2mp_core.llm import get_chat_model
from fp2mp_core.nodes.context import wiki_briefing
from fp2mp_core.state import BlackBoard

_SYSTEM = """\
You are the Mediator. Write a comprehensive, well-structured answer to the
ORIGINAL question using ONLY the knowledge provided (wiki briefing + confirmed
facts). This works for ANY domain — do not assume one and do not use a
domain-specific template.

Hard rules (fail-open — always commit):
- ALWAYS commit to a concrete, specific, best-supported answer. If the question
  asks to choose / find / decide / locate / design, NAME the specific option,
  place, value or design explicitly. A list of criteria or a methodology is
  NOT an acceptable answer on its own.
- NEVER refuse, never reply that evidence is insufficient, never replace the
  answer with "open questions". If evidence is thin, still give the single best
  weighted answer and lower the stated confidence.
- Express uncertainty inline: a short "Confidence: <low/medium/high> — <one
  caveat>" rather than deferring the decision.
- Ground the answer in the provided knowledge; prefer computed/normative facts
  over web; be specific with figures, names and identifiers that appear.

Write the answer directly in Markdown (no preamble). Lead with the committed
answer in the first 1-3 sentences, then the supporting rationale.
"""


# Conservative: only the clearest refusal openers (won't match committed,
# merely-hedged answers — those don't start by declining to answer).
_REFUSAL = re.compile(
    r"^\W*(i('?m| am)?\s+(unable|sorry|cannot|can'?t)\b"
    r"|i (cannot|can'?t|am unable to)\s+(provide|answer|determine)"
    r"|unable to (provide|answer|determine)"
    r"|insufficient (evidence|data|information) to (answer|provide)"
    r"|не могу\s+(дать|предоставить|ответить)"
    r"|недостаточно (данных|информации) (чтобы|для))",
    re.IGNORECASE,
)


def _looks_like_refusal(text: str) -> bool:
    head = (text or "").strip()[:200]
    return bool(_REFUSAL.search(head))


def _fallback_draft(question: str, facts: list) -> str:
    """Never return empty — commit from whatever facts exist."""
    if facts:
        best = max(facts, key=lambda f: f.get("confidence", 0.0))
        lead = best.get("claim", "").strip()
        conf = best.get("confidence", 0.0)
        bullets = "\n".join(
            f"- {f.get('claim','')[:240]}" for f in facts[:6] if f.get("claim")
        )
        return (
            f"## Answer\n\n{lead}\n\n"
            f"Confidence: {'low' if conf < 0.5 else 'medium' if conf < 0.75 else 'high'} "
            f"— synthesized from the available evidence.\n\n"
            f"### Supporting evidence\n{bullets}"
        )
    return (
        f"## Answer\n\nBased on the limited evidence gathered, the best-supported "
        f"working answer to: \"{question}\" is the most plausible option implied "
        f"by the analysis so far. Confidence: low — evidence was sparse; treat "
        f"as a provisional but actionable conclusion."
    )


def answer_commit_node(state: BlackBoard) -> dict[str, Any]:
    """Produce a committed, concrete weighted answer (never empty/refusing)."""
    question = state.get("question", "")
    facts = state.get("output", [])
    critique = state.get("critique", {})
    contradictions = critique.get("contradictions", []) if critique else []

    briefing = wiki_briefing(state, limit=3500)
    facts_str = "\n".join(
        f"- [{f.get('sub_query_id','?')} conf={f.get('confidence',0):.2f} "
        f"{f.get('source_type','')}] {f.get('claim','')[:300]}"
        for f in sorted(facts, key=lambda x: x.get("confidence", 0), reverse=True)[:20]
    )
    contradictions_str = "\n".join(f"- {c}" for c in contradictions) or "None."

    prompt = f"""\
ORIGINAL QUESTION: {question}

CONFIRMED FACTS:
{facts_str or "None promoted — use the briefing and commit to the best weighted answer."}

KNOWLEDGE BRIEFING:
{briefing or "(sparse)"}

UNRESOLVED CONTRADICTIONS:
{contradictions_str}

Write the committed, concrete weighted answer now.
"""

    draft = ""
    try:
        llm = get_chat_model(temperature=0.3)
        resp = llm.invoke([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ])
        draft = (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        logger.info("answer_commit LLM failed (%s); using fallback", exc)

    if not draft or _looks_like_refusal(draft):
        logger.info("answer_commit | empty/refusal draft → fail-open fallback")
        draft = _fallback_draft(question, facts)

    logger.info("answer_commit | facts=%d draft_len=%d", len(facts), len(draft))
    return {
        "draft_answer": draft,
        "current_stage": "answer_committed",
        "agent_trace": [
            {"node": "answer_commit", "facts": len(facts), "draft_len": len(draft)}
        ],
    }
