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

_FAILED_CLAIM_PREFIXES = (
    "Vector search error",
    "Search failed",
    "WebSearchAgent did not produce usable evidence",
    "Failed to fetch",
    "Failed to research",
    "Research fetch failed",
    "Normative search failed",
    "NormativeAgent did not produce usable evidence",
    "CodeSpatialAgent did not complete",
    "CodeSpatialAgent low-confidence augmentation only",
)

_SYNTHESIS_SYSTEM = """\
You are producing the FINAL answer to an open-ended question. Your job is to
COMMIT to a concrete, direct answer while showing high-quality reasoning.

You receive:
- The original question
- Confirmed facts (with confidence scores and source attributions)
- A synthesis narrative from the MediatorAgent
- Unresolved contradictions (if any)

Rules:
- Lead with a direct, concrete answer to exactly what was asked. If the question
  asks which / where / how many / what should, give the specific choice,
  location, number, or recommendation. Never answer with "it is necessary to
  analyze" or "further data is required" in place of an answer.
- Even under partial or conflicting evidence, pick the single best-supported
  answer (weighted by confidence and source agreement) and state it as your
  conclusion. Commit; do not refuse, defer, or hedge the conclusion itself.
- Support the answer with the confirmed facts and cite sources inline.
- Keep all sections domain-neutral and factual.

Write as a thoughtful expert would — a decision memo, not a graded checklist.
Use these section headers (markdown):
1. **Answer** — the committed conclusion in 1-3 sentences (mandatory, first)
2. **How I'm reading the question** — the problem frame and why it fits
3. **What it hinges on** — the main components the answer depends on
4. **Options I weighed** — the genuine alternatives, perspectives, or paths, and
   why the chosen one wins over them
5. **The case for it** — why the conclusion follows from the confirmed facts,
   with [source] citations and the relevant numbers/constraints; show how the
   parts fit together and how facts, constraints, and knowledge combine into one
   robust answer (do not split this into separate "coherence" or "knowledge"
   sections — weave them in)
6. **What's still uncertain** — material uncertainty, confidence, and what
   remains unstable
7. **Where this reasoning could be wrong** — a reasoning-level self-assessment,
   not domain hedging and not a second uncertainty section. Include: rejected
   alternative framings and what would break/change if one were true; calibrated
   confidence for 2-3 load-bearing claims plus exactly where each may be wrong;
   bias introduced by how the evidence itself was gathered or selected (source-type
   skew, search/retrieval skew, measurement/proxy skew, agent-routing skew), not
   only subject-matter uncertainty; the weakest reasoning link and the
   highest-leverage missing work or information; residual risks if acting on the
   answer as written.

These are properties of strong reasoning and should emerge through substance,
not as labeled sections: a precise problem frame, a clear breakdown, honest
treatment of real alternatives, evidence-backed commitment, internal consistency,
candid uncertainty, integration of what is known, and a genuine self-critique of
the reasoning itself. Do not write to a checklist or name sections after these
qualities. The final section must inspect the reasoning that led to the answer —
it must not replace or weaken the committed Answer.

Stay factual and grounded in the provided facts. The answer must remain
committed regardless of the question's domain. Use neutral, substantive language;
do not use hedging as a substitute for analysis.
"""

_CRITIQUE_SYSTEM = """\
You are a strict critic of a final-answer draft. Your single question: does this
read as genuine, committed expert reasoning — or as a hedge, a checklist, or a
restated question?

Judge the draft against the marks of strong reasoning (as substance, not as
labeled sections):
- The problem frame is precise and fits the question.
- The reasoning breaks the problem into useful parts.
- It weighs genuinely different alternatives, not strawmen.
- The conclusion follows from evidence, trade-offs, numbers, and constraints.
- The parts connect into one non-contradictory answer.
- Uncertainty is material, bounded, and tied to what to do about it.
- Facts, constraints, and knowledge are synthesized, not just listed.
- The final section critiques the reasoning process itself.

The self-critique (final section, "Where this reasoning could be wrong") is the
hardest test. It fails if any of these is missing:
- It is about the reasoning process, not merely reflection about the domain.
- It names rejected alternative framings and how the conclusion would change if
  one were correct.
- It calibrates confidence for 2-3 load-bearing claims and states where each may
  be wrong.
- It identifies bias introduced by how the evidence itself was gathered/selected.
- It names the weakest reasoning link, the highest-leverage missing
  information/work, and residual risks of acting on the answer.

Flag a weak self-critique explicitly. Do not relax the other marks; the answer
must stay committed, direct, and evidence-grounded.

Return concise, actionable critique bullets only, each naming the specific
weakness so the refiner can target it.
"""

_REFINE_SYSTEM = """\
You are refining the final answer after critique. Preserve the committed Answer
and fix every weakness the critique raised.

Treat the critique as a repair list. Strengthen each flagged spot in place,
through substance — do not add labeled sections or write to a checklist.

If the self-critique ("Where this reasoning could be wrong") was flagged, deepen
it with the missing reasoning-level elements: rejected alternative framings,
calibrated confidence for load-bearing claims, bias from how the evidence was
gathered, the weakest link plus the highest-leverage gap, and residual risks. Do
not turn it into domain hedging or a refusal.

This is a Pareto improvement, not a rebalancing: do not trim or weaken any part
that was already strong while fixing another.

Return only the final markdown answer using the same memo section headers
(Answer; How I'm reading the question; What it hinges on; Options I weighed; The
case for it; What's still uncertain; Where this reasoning could be wrong).
"""


def final_synthesis_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node — produces the final answer."""
    question = state.get("question", "")
    output_facts = [f for f in state.get("output", []) if not _is_failed_claim(f.get("claim", ""))]
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

    synthesis_prompt = f"""\
Question: {question}

Confirmed facts:
{facts_str or "No confirmed facts."}

Mediator synthesis:
{synthesis_text[:1500] if synthesis_text else "Not available."}

Unresolved contradictions:
{contradictions_str}

Overall confidence: {overall_conf:.2f}
Iterations used: {iteration}

Produce the final answer now using the memo section headers. Lead with the
direct, concrete Answer to the question. Commit to a single best-supported
conclusion; do not defer or hedge.
"""

    try:
        llm = get_chat_model(temperature=0.1)
        final_answer = _run_synthesis_chain(llm, synthesis_prompt)
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


def _response_text(response: Any) -> str:
    return response.content if hasattr(response, "content") else str(response)


def _is_failed_claim(text: str) -> bool:
    return str(text or "").strip().startswith(_FAILED_CLAIM_PREFIXES)


def _run_synthesis_chain(llm: Any, synthesis_prompt: str) -> str:
    draft_response = llm.invoke([
        {"role": "system", "content": _SYNTHESIS_SYSTEM},
        {"role": "user", "content": synthesis_prompt},
    ])
    draft = _response_text(draft_response)

    critique_prompt = f"""\
Original synthesis prompt:
{synthesis_prompt}

Draft answer:
{draft}

Critique the draft on whether it reads as genuine, committed expert reasoning.
Treat the self-critique ("Where this reasoning could be wrong") as a strict test:
if it is domain reflection rather than reasoning-level self-assessment, lacks
rejected framings, lacks calibrated confidence for each load-bearing claim, lacks
analysis of bias in how evidence was gathered, or lacks weakest-link/
highest-leverage-gap analysis, flag it explicitly. Do not weaken the other
strengths or the committed Answer.
"""
    critique_response = llm.invoke([
        {"role": "system", "content": _CRITIQUE_SYSTEM},
        {"role": "user", "content": critique_prompt},
    ])
    critique = _response_text(critique_response)

    refine_prompt = f"""\
Original synthesis prompt:
{synthesis_prompt}

Draft answer:
{draft}

Critique:
{critique}

Refine the draft into the final answer. If the self-critique ("Where this
reasoning could be wrong") was flagged, deepen it with the missing reasoning-level
elements. This is a Pareto improvement, not a rebalancing: do not trim or weaken
any part that was already strong. If any other spot was flagged, strengthen it in
place while preserving its role. Keep the Answer committed and avoid fail-closed
language.
"""
    refine_response = llm.invoke([
        {"role": "system", "content": _REFINE_SYSTEM},
        {"role": "user", "content": refine_prompt},
    ])
    return _response_text(refine_response)


def _fallback_answer(
    question: str,
    facts: list,
    contradictions: list[str],
    overall_conf: float,
) -> str:
    facts = [f for f in facts if not _is_failed_claim(f.get("claim", ""))]
    best_fact = max(facts, key=lambda x: x.get("confidence", 0), default={})
    best_claim = (
        best_fact.get("claim")
        or "The best-supported answer follows from the confirmed facts below."
    )
    fact_lines = []
    for f in facts:
        claim = f.get("claim", "")
        conf = f.get("confidence", 0.0)
        agents = ", ".join(f.get("source_agents", []))
        fact_lines.append(f"- [{conf:.2f}] {claim} *(sources: {agents})*")

    contradiction_lines = [f"- {c}" for c in contradictions] or [
        "- No unresolved contradictions were reported."
    ]
    lines = [
        "## Answer\n",
        f"{best_claim}\n",
        "## How I'm reading the question\n",
        "I read this as a call to commit to the strongest supported conclusion "
        f"for: {question}\n",
        "## What it hinges on\n",
        "- Which confirmed facts carry the most weight.\n"
        "- Whether any unresolved contradiction defeats the leading claim.\n"
        "- Committing to the best-supported conclusion rather than deferring.\n",
        "## Options I weighed\n",
        "- Follow the strongest confirmed fact pattern (chosen).\n"
        "- Give more weight to unresolved contradictions if they directly "
        "undermine the main claim (rejected unless a contradiction is decisive).\n",
        "## The case for it\n",
    ]
    lines.extend(fact_lines or ["- No confirmed facts were available."])
    lines.append(
        "\nThe conclusion follows the highest-confidence available claims and "
        "integrates source agreement and confidence into one committed answer, "
        "keeping contradictions visible rather than letting them erase it.\n"
    )
    lines.extend([
        "## What's still uncertain\n",
        f"Overall confidence is {overall_conf:.2f}. Contradictions and "
        "low-confidence facts remain the main uncertainty drivers.\n",
        "Open contradictions:\n",
    ])
    lines.extend(contradiction_lines)
    lines.extend([
        "\n## Where this reasoning could be wrong\n",
        "- Rejected framings: A conservative evidence-only frame would withhold "
        "judgment, but committing to the best-supported conclusion is required "
        "here. A contradiction-first frame would change the answer only if an "
        "unresolved contradiction directly defeats the highest-confidence claim.",
        f"- Calibrated confidence: The main conclusion sits at {overall_conf:.2f}; "
        "it may be wrong where the top confirmed fact is incomplete, stale, or "
        "source-skewed. The contradiction assessment is moderate-confidence unless "
        "all conflicting claims share comparable source quality. The source-weighting "
        "step is weaker if agent outputs used overlapping sources.",
        "- Bias from how evidence was gathered: this fallback leans on whatever "
        "facts were confirmed, so it may inherit source-type skew, agent-selection "
        "skew, and over-weighting of easily retrieved evidence.",
        "- Weakest link and highest-leverage gap: the weakest link is whether the "
        "highest-confidence fact captures the decisive constraint. The highest-leverage "
        "next check is an independent source or computation aimed directly at that "
        "decisive constraint.",
        "- Residual risks: acting as written may miss edge cases, minority evidence, "
        "or late-breaking facts not captured in the confirmed-fact set.\n",
    ])

    return "\n".join(lines)
