"""
ReDI re-planner — makes decomposition iterative (the gemini pattern).

Given the original question, the current sub-query decomposition with its
coverage, the accumulated knowledge, and the critic's feedback, this revises
the decomposition so the system can actually answer the ORIGINAL question.

Domain-neutral by design: it never assumes a domain or a fixed solution
template — it only reasons about what is still unknown.
"""

from __future__ import annotations

import json

from langchain_core.prompts import ChatPromptTemplate

from fp2mp_core.llm import get_chat_model
from fp2mp_core.redi.decomposer import _SubQuerySpec
from fp2mp_core.state import SubQuery

_SYSTEM = """\
You are a reasoning-driven RE-PLANNER for open-ended questions of ANY domain.
Decomposition is not frozen: given new evidence and the critic's feedback, you
revise it so the system can actually answer the ORIGINAL question.

You may:
- Keep already-covered sub-queries UNCHANGED (same sub_query_id and text).
- Refine sub-queries that are vague or still unanswered.
- ADD new sub-queries for genuine gaps the feedback or evidence reveals.
- Set "depends_on" (sub_query_ids that must be answered first; [] if independent).

Rules:
- Stay domain-neutral. Do not impose any domain-specific template.
- Preserve the sub_query_id of every covered sub-query exactly.
- New sub-queries get fresh ids continuing the numbering (sq_010, sq_011, ...).
- Keep exactly one final integrative sub-query with intent_aspect="recommendation"
  (search_modality="any", evidence_type="factual") depending on the others, that
  COMMITS to the single best-supported concrete answer/decision for the original
  question (a specific option/value, not a list of criteria).
- Do NOT exceed {max_sq} sub-queries total. Prefer sharpening over proliferation.
- Output ONLY a valid JSON array — no markdown, no commentary.
"""

_USER = """\
ORIGINAL QUESTION: {question}

CURRENT DECOMPOSITION (id | coverage | evidence_type | text):
{current}

CRITIC FEEDBACK:
{feedback}

ACCUMULATED KNOWLEDGE (compact):
{briefing}

Revise the decomposition. Output JSON array of sub-query objects:
[
  {{
    "sub_query_id": "sq_001",
    "text": "<focused sub-question>",
    "intent_aspect": "<short aspect label>",
    "search_modality": "web|normative|code|any",
    "evidence_type": "factual|empirical|normative",
    "depends_on": [],
    "independence": true
  }}
]
"""


class ReDIReplanner:
    def __init__(self, model_id: str | None = None, max_sub_queries: int = 8) -> None:
        self._max = max_sub_queries
        self._llm = get_chat_model(model_id=model_id, temperature=0.0)
        self._prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM), ("human", _USER)]
        )
        self._chain = self._prompt | self._llm

    def __call__(
        self,
        question: str,
        current_sub_queries: list[SubQuery],
        coverage: dict[str, str],
        feedback: str,
        briefing: str,
    ) -> list[SubQuery]:
        current_lines = "\n".join(
            f"- {sq.get('sub_query_id')} | {coverage.get(sq.get('sub_query_id',''), 'pending')}"
            f" | {sq.get('evidence_type','factual')} | {sq.get('text','')}"
            for sq in current_sub_queries
        )
        response = self._chain.invoke(
            {
                "question": question,
                "current": current_lines or "(none)",
                "feedback": feedback or "(no specific feedback)",
                "briefing": (briefing or "(none)")[:1200],
                "max_sq": self._max,
            }
        )
        text = response.content if hasattr(response, "content") else str(response)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.rstrip("`").strip()

        raw = json.loads(text)
        if isinstance(raw, dict) and "sub_queries" in raw:
            raw = raw["sub_queries"]

        for item in raw:
            if item.get("search_modality") in {"factual", "empirical"}:
                item.setdefault("evidence_type", item["search_modality"])
                item["search_modality"] = (
                    "code" if item["evidence_type"] == "empirical" else "web"
                )
            if item.get("search_modality") == "normative":
                item.setdefault("evidence_type", "normative")
            item.setdefault("evidence_type", "factual")

        specs = [_SubQuerySpec(**item) for item in raw][: self._max]
        valid_ids = {s.sub_query_id for s in specs}
        return [
            SubQuery(
                sub_query_id=s.sub_query_id,
                text=s.text,
                intent_aspect=s.intent_aspect,
                search_modality=s.search_modality,
                evidence_type=s.evidence_type,
                depends_on=[d for d in s.depends_on if d in valid_ids and d != s.sub_query_id],
                independence=s.independence,
                enriched_variants=[],
                keywords=[],
                domain_hints=[],
            )
            for s in specs
        ]
