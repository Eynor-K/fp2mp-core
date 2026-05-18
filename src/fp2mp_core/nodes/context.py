"""
Shared context helpers — used by multiple graph nodes.

Functions here read BlackBoard state and return compact summaries
that agents and routing nodes consume without touching the full state.
"""

from __future__ import annotations

import re

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard

_VALID_AGENTS = {
    "WebSearchAgent",
    "NormativeAgent",
    "CodeSpatialAgent",
    "BlocksNetAgent",
}


def parse_follow_ups(text: str) -> list[dict[str, str]]:
    """Parse optional 'FOLLOW_UP: <AgentName> | <task>' lines from agent output.

    Domain-neutral handoff mechanism so agents can propose concrete next steps
    for another agent. Invalid / empty suggestions are ignored.
    """
    suggestions: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^FOLLOW[_ ]?UP\s*:\s*(.+)$", line, re.IGNORECASE)
        if not m:
            continue
        body = m.group(1)
        if "|" not in body:
            continue
        agent, directive = (p.strip() for p in body.split("|", 1))
        if agent in _VALID_AGENTS and len(directive) >= 8:
            suggestions.append({"assigned_agent": agent, "directive": directive})
    return suggestions[:3]

# ---------------------------------------------------------------------------
# Question intent classification
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = """\
Classify the question intent in one word:
- "planning": asks where/how to build, allocate, improve, or choose locations
- "analytical": asks what is better/worse, rankings, comparisons, quantitative comparisons
- "spatial": asks about geographic features, locations, routes, distances
- "regulatory": asks about rules, laws, standards, requirements
- "factual": asks what something is, when it happened, who did it

Reply with exactly one word from the list above.
"""


def _heuristic_question_intent(question: str) -> str | None:
    q = question.lower()
    if any(x in q for x in ("где постро", "куда размест", "что выбрать", "how to improve")):
        return "planning"
    if any(x in q for x in ("норм", "закон", "санпин", "гост", "снип", "regulation")):
        return "regulatory"
    if any(x in q for x in ("расстоя", "маршрут", "координ", "улиц", "район", "spatial")):
        return "spatial"
    if any(x in q for x in ("сравн", "рейтинг", "лучше", "хуже", "сколько", "метрик")):
        return "analytical"
    if len(q.split()) <= 2:
        return "factual"
    return None


def classify_question_intent(question: str) -> str:
    heuristic = _heuristic_question_intent(question)
    if heuristic:
        return heuristic
    try:
        llm = get_chat_model(temperature=0.0)
        resp = llm.invoke([
            {"role": "system", "content": _INTENT_SYSTEM},
            {"role": "user", "content": question},
        ])
        intent = resp.content.strip().lower() if hasattr(resp, "content") else str(resp).strip()
        if intent in {"planning", "analytical", "spatial", "regulatory", "factual"}:
            return intent
    except Exception:
        pass
    return "factual"


# ---------------------------------------------------------------------------
# Modality → default agent mapping
# ---------------------------------------------------------------------------

def _modality_to_agent(modality: str) -> str:
    return {
        "web": "WebSearchAgent",
        "normative": "NormativeAgent",
        "code": "CodeSpatialAgent",
        "any": "CodeSpatialAgent",
    }.get(modality, "CodeSpatialAgent")


# ---------------------------------------------------------------------------
# Wiki briefing — compact context string for nodes
# ---------------------------------------------------------------------------

def _bm25_top_wiki_pages(wiki: dict, question: str, top_k: int = 3) -> list[tuple[str, dict]]:
    """Return top_k wiki pages most relevant to the question using BM25."""
    _SKIP = {"index.md", "log.md"}
    pages = [(pid, p) for pid, p in wiki.items() if pid not in _SKIP and p.get("content")]
    if not pages:
        return []
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
        tokenized = [p.get("content", "").lower().split() for _, p in pages]
        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(question.lower().split())
        ranked = sorted(zip(scores, pages), key=lambda x: x[0], reverse=True)
        return [(pid, page) for score, (pid, page) in ranked[:top_k] if score > 0]
    except ImportError:
        return sorted(pages, key=lambda x: x[1].get("confidence", 0), reverse=True)[:top_k]


def wiki_briefing(state: BlackBoard, limit: int = 3000) -> str:
    """
    Compact context string: index + BM25-ranked wiki pages + confirmed facts + raw tail.
    Used by Orchestrator, Mediator, Critic, and Hypothesis nodes.
    """
    question = state.get("question", "")
    wiki = state.get("wiki", {})
    parts: list[str] = []

    index_page = wiki.get("index.md")
    if index_page:
        parts.append("## Wiki Index\n" + index_page.get("content", "")[:600])

    if question and wiki:
        top_pages = _bm25_top_wiki_pages(wiki, question, top_k=3)
        if top_pages:
            parts.append("\n## Most Relevant Wiki Pages")
            for pid, page in top_pages:
                title = page.get("title", pid)
                conf = page.get("confidence", 0.0)
                snippet = page.get("content", "")[:300].replace("\n", " ")
                parts.append(f"### {title} [conf={conf:.2f}]\n{snippet}")

    output = state.get("output", [])
    if output:
        parts.append("\n## Confirmed Facts")
        for f in output[:10]:
            claim = f.get("claim", "")
            conf = f.get("confidence", 0.0)
            sq = f.get("sub_query_id", "?")
            src = f.get("source_type", "")
            parts.append(f"- [{sq} conf={conf:.2f} {src}] {claim[:200]}")

    raw = state.get("raw_data", [])
    recent = [r for r in raw if not r.get("curated", False)][-5:]
    if recent:
        parts.append("\n## Recent Raw Entries (not yet curated)")
        for r in recent:
            agent = r.get("agent", "?")
            rtype = r.get("type", "?")
            snippet = r.get("content", "")[:200].replace("\n", " ")
            parts.append(f"- [{agent}/{rtype}] {snippet}")

    return "\n".join(parts)[:limit]


# ---------------------------------------------------------------------------
# Coverage map
# ---------------------------------------------------------------------------

def coverage_from_sub_queries(state: BlackBoard) -> dict[str, str]:
    """Build coverage map: sub_query_id → 'covered' | 'partial' | 'pending'."""
    sub_queries = state.get("redi_decomposition", [])
    output = state.get("output", [])

    coverage: dict[str, str] = {}
    for sq in sub_queries:
        sq_id = sq["sub_query_id"]
        ev_type = sq.get("evidence_type", "factual")
        relevant = [f for f in output if f.get("sub_query_id") == sq_id]
        if not relevant:
            coverage[sq_id] = "pending"
            continue

        best_conf = max((f.get("confidence", 0.0) for f in relevant), default=0.0)
        if ev_type == "empirical":
            quantitative_agents = {"BlocksNetAgent", "CodeSpatialAgent"}
            quantitative_facts = [
                f for f in relevant
                if f.get("source_type") == "computed"
                or set(f.get("source_agents", [])) & quantitative_agents
            ]
            quant_conf = max(
                (f.get("confidence", 0.0) for f in quantitative_facts),
                default=0.0,
            )
            coverage[sq_id] = "covered" if quant_conf >= 0.7 else "partial"
        else:
            coverage[sq_id] = "covered" if best_conf >= 0.7 else "partial"
    return coverage
