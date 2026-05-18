"""
ReDI Stage A: Decompose a complex open-ended question into 3-5 independent sub-queries.

Each sub-query covers exactly one aspect of the original question and has a
designated search modality so the Orchestrator can assign the right agent.
"""

from __future__ import annotations

import json

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import SubQuery

_SYSTEM = """\
You are a reasoning-driven decomposition expert for open-ended questions of ANY
domain (engineering, science, policy, urban, business, etc.). Do not assume a
specific domain or impose a fixed solution template.

Think first, then decompose:
1. REASON about what is genuinely unknown — what must be established to answer
   the original question well? Identify the key unknowns / hypotheses.
2. Turn each distinct unknown into one focused sub-query. Produce as many as the
   question truly needs (typically 3-6; more only if genuinely multi-faceted).
3. Sub-queries MAY depend on each other. If a sub-query can only be answered
   once another is resolved, list those sub_query_ids in "depends_on".
   Independent sub-queries have "depends_on": [].
4. Assign each sub-query a search_modality (a routing hint, not a constraint):
   - "web"       → general factual information from the internet
   - "normative" → laws, regulations, standards, codes (SNiP/GOST/SanPiN or any)
   - "code"      → quantitative calculation, data analysis, simulation, or
                   determining which items satisfy a computed filter.
                   Examples (domain-neutral):
                   "Compute the load distribution under condition X" → code
                   "Which records in the dataset exceed threshold Y?" → code
                   "Simulate the model and report the steady state" → code
   - "any"       → modality unclear; the Orchestrator will decide
5. Assign an intent_aspect label (short phrase naming the dimension covered).
6. Assign evidence_type:
   - "empirical": requires computation, measurement, or data analysis
   - "normative": requires laws, standards, regulations
   - "factual": everything else (descriptions, history, mechanisms, options)
   Use whichever genuinely fits — do NOT force any particular mix.
7. Add exactly ONE final integrative sub-query with intent_aspect="recommendation",
   search_modality="any", evidence_type="factual", that depends_on the others and
   asks: given all gathered findings, COMMIT to the single best-supported,
   concrete answer/decision for the ORIGINAL question — name the specific
   option/value/choice explicitly, not a list of criteria. (Universal — every
   open-ended inquiry ends with one committed conclusion.)
8. Output ONLY valid JSON matching the schema below — no markdown, no commentary.
"""

_USER = """\
Question: {question}

Output JSON (array of sub-query objects):
[
  {{
    "sub_query_id": "sq_001",
    "text": "<focused sub-question>",
    "intent_aspect": "<short aspect label>",
    "search_modality": "web|normative|code|any",
    "evidence_type": "factual|empirical|normative",
    "depends_on": [],
    "independence": true
  }},
  ...
]
"""


class _SubQuerySpec(BaseModel):
    sub_query_id: str
    text: str
    intent_aspect: str
    search_modality: str = Field(pattern=r"^(web|normative|code|any)$")
    evidence_type: str = Field(default="factual", pattern=r"^(factual|empirical|normative)$")
    depends_on: list[str] = Field(default_factory=list)
    independence: bool = True


class _SubQuerySet(BaseModel):
    sub_queries: list[_SubQuerySpec]


class ReDIDecomposer:
    def __init__(self, model_id: str | None = None) -> None:
        self._llm = get_chat_model(model_id=model_id, temperature=0.0)
        self._prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM), ("human", _USER)]
        )
        self._chain = self._prompt | self._llm

    def __call__(self, question: str) -> list[SubQuery]:
        response = self._chain.invoke({"question": question})
        text = response.content if hasattr(response, "content") else str(response)

        # Strip markdown code fences if present
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
                item["search_modality"] = "code" if item["evidence_type"] == "empirical" else "web"
            if item.get("search_modality") == "normative":
                item.setdefault("evidence_type", "normative")
            item.setdefault("evidence_type", "factual")

        specs = [_SubQuerySpec(**item) for item in raw]
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
