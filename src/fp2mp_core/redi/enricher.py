"""
ReDI Stage B: Semantically enrich each sub-query with variants, keywords, and domain hints.

Enrichment improves recall in downstream retrieval by generating rephrasings,
synonyms, and domain-specific terminology before any external calls are made.
"""

from __future__ import annotations

import json

from langchain_core.prompts import ChatPromptTemplate

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import SubQuery

_SYSTEM = """\
You are a semantic enrichment specialist. For a given search sub-query you produce:
1. Three alternative phrasings (synonyms, domain restatements, broader/narrower forms).
2. A list of 5-10 key terms useful for keyword-based scoring.
3. Domain hints — domain-specific identifiers (e.g. "СНиП", "ГОСТ", "SanPiN") relevant
   for normative sub-queries, or library names for code sub-queries. Empty list for web sub-queries.

Output ONLY valid JSON — no markdown, no commentary.
"""

_USER = """\
Sub-query:
  id: {sub_query_id}
  text: {text}
  intent_aspect: {intent_aspect}
  search_modality: {search_modality}

Output JSON:
{{
  "sub_query_id": "{sub_query_id}",
  "variants": ["<variant 1>", "<variant 2>", "<variant 3>"],
  "keywords": ["kw1", "kw2", ...],
  "domain_hints": ["hint1", ...]
}}
"""


class ReDIEnricher:
    def __init__(self, model_id: str | None = None) -> None:
        self._llm = get_chat_model(model_id=model_id, temperature=0.2)
        self._prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM), ("human", _USER)]
        )

    def enrich(self, sub_query: SubQuery) -> SubQuery:
        chain = self._prompt | self._llm
        response = chain.invoke(
            {
                "sub_query_id": sub_query["sub_query_id"],
                "text": sub_query["text"],
                "intent_aspect": sub_query.get("intent_aspect", ""),
                "search_modality": sub_query.get("search_modality", "any"),
            }
        )
        text = response.content if hasattr(response, "content") else str(response)
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()

        data = json.loads(text)
        enriched = dict(sub_query)
        enriched["enriched_variants"] = data.get("variants", [])
        enriched["keywords"] = data.get("keywords", [])
        enriched["domain_hints"] = data.get("domain_hints", [])
        return SubQuery(**enriched)  # type: ignore[arg-type]

    def enrich_all(self, sub_queries: list[SubQuery]) -> list[SubQuery]:
        return [self.enrich(sq) for sq in sub_queries]
