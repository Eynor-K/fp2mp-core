"""
FinalSynthesis node — converts confirmed facts and wiki into a structured markdown answer.

Called after the loop exits (CriticAgent decided STOP or iteration limit reached).
Optionally persists wiki to disk if WIKI_PERSIST_DIR is configured.
"""

from __future__ import annotations

from typing import Any

from fp2mp_core.config import get_settings
from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard

_SYNTHESIS_SYSTEM = """\
You are producing the FINAL answer to an open-ended question. Your job is to
COMMIT to a concrete, direct answer — not to survey the problem space.

You receive:
- The original question
- Confirmed facts (with confidence scores and source attributions)
- A synthesis narrative from the MediatorAgent
- Unresolved contradictions (if any)

Rules:
- Lead with a direct, concrete answer to exactly what was asked, in the first
  1-3 sentences. If the question asks which / where / how many / what should —
  give the specific choice, location, number, or recommendation. Never answer
  with "it is necessary to analyze" or "further data is required" in place of
  an answer.
- Even under partial or conflicting evidence, pick the single best-supported
  answer (weighted by confidence and source agreement) and state it as your
  conclusion. Commit; do not refuse, defer, or hedge the conclusion itself.
- Support the answer with the confirmed facts and cite sources inline.
- If material uncertainty exists, condense it into at most ONE short closing
  sentence (e.g. "Confidence is moderate; the main caveat is X."). Do NOT
  produce dedicated "Limitations", "Uncertainties", or "Open Questions"
  sections.

Output (markdown):
1. **Answer** — the direct, concrete answer in 1-3 sentences (mandatory, first)
2. **Reasoning** — why this answer follows from the confirmed facts, with
   [source] citations and the relevant numbers/constraints
3. One optional closing caveat sentence, only if a material risk exists

Stay factual and grounded in the provided facts. The answer must remain
committed regardless of the question's domain.
"""


def final_synthesis_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node — produces the final answer."""
    question = state.get("question", "")
    output_facts = state.get("output", [])
    wiki = state.get("wiki", {})
    critique = state.get("critique", {})

    # Build facts string
    facts_str = ""
    for f in sorted(output_facts, key=lambda x: x.get("confidence", 0), reverse=True):
        sq = f.get("sub_query_id", "?")
        conf = f.get("confidence", 0.0)
        agents = ", ".join(f.get("source_agents", []))
        claim = f.get("claim", "")
        limitations = "; ".join(f.get("limitations", []))
        cit_urls = [c.get("url", c.get("document", "")) for c in f.get("citations", [])]
        cit_str = " | ".join(filter(None, cit_urls))
        facts_str += (
            f"\n- [{sq} conf={conf:.2f} src={agents}] {claim}"
            + (f"\n  Limitations: {limitations}" if limitations else "")
            + (f"\n  Sources: {cit_str}" if cit_str else "")
        )

    synthesis_page = wiki.get("synthesis", {})
    synthesis_text = synthesis_page.get("content", "") if synthesis_page else ""

    contradictions = critique.get("contradictions", [])
    contradictions_str = "\n".join(f"- {c}" for c in contradictions) if contradictions else "None"

    overall_conf = critique.get("overall_confidence", 0.0)
    iteration = state.get("iteration", 0)

    prompt = f"""\
Question: {question}

Confirmed facts:
{facts_str or "No confirmed facts."}

Mediator synthesis:
{synthesis_text[:1500] if synthesis_text else "Not available."}

Unresolved contradictions:
{contradictions_str}

Overall confidence: {overall_conf:.2f}
Iterations used: {iteration}

Produce the final answer now. Lead with the direct, concrete answer to the question, then the reasoning. Commit to a single best-supported conclusion; do not defer or hedge.
"""

    try:
        llm = get_chat_model(temperature=0.1)
        response = llm.invoke([
            {"role": "system", "content": _SYNTHESIS_SYSTEM},
            {"role": "user", "content": prompt},
        ])
        final_answer = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        # Fallback: format facts directly
        final_answer = _fallback_answer(question, output_facts, contradictions, overall_conf)
        final_answer += f"\n\n*Note: LLM synthesis failed: {exc}*"

    # Optionally persist wiki
    settings = get_settings()
    if settings.wiki_persist_dir:
        try:
            from fp2mp_core.tools.wiki_io import persist_wiki
            persist_wiki(wiki, settings.wiki_persist_dir)
        except Exception:
            pass

    return {
        "final_answer": final_answer,
        "current_stage": "finished",
        "stop_flag": True,
    }


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
