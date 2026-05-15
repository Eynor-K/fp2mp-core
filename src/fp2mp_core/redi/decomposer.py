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
You are a query decomposition expert. Your task is to break down complex, open-ended questions
into independent sub-queries — one per distinct aspect that needs to be investigated.

Rules:
1. Produce 3-5 sub-queries (more only if the question is genuinely multi-faceted).
2. Each sub-query must be answerable independently (no sub-query depends on another's answer).
3. Assign each sub-query a search_modality:
   - "web"       → general factual information from the internet
   - "normative" → laws, regulations, standards, SNiP/GOST/SanPiN or similar
   - "code"      → quantitative calculation, spatial analysis, numeric reasoning, OR any
                   question about which specific streets/objects/zones exist in a named
                   geographic area. These are answerable via OpenStreetMap, not web search.
                   Examples:
                   "Which streets in district X are pedestrian?" → code
                   "How many bike lanes exist in area Y?" → code
                   "What is the walkable area within 500m of point Z?" → code
   - "any"       → the modality is unclear; Orchestrator will decide
4. Assign an intent_aspect label (short phrase describing what dimension this covers).
5. Output ONLY valid JSON matching the schema below — no markdown, no commentary.
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

        specs = [_SubQuerySpec(**item) for item in raw]
        return [
            SubQuery(
                sub_query_id=s.sub_query_id,
                text=s.text,
                intent_aspect=s.intent_aspect,
                search_modality=s.search_modality,
                independence=s.independence,
                enriched_variants=[],
                keywords=[],
                domain_hints=[],
            )
            for s in specs
        ]
