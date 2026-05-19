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

Output (markdown):
1. **Direct Answer** — the committed answer in 1-3 sentences (mandatory, first)
2. **Framing** — define the decision/problem frame and why it fits the question
3. **Decomposition** — break the answer into the main analytical components
4. **Candidate Views** — compare plausible alternatives, perspectives, or paths
5. **Evidence And Justification** — explain why the chosen answer follows from
   the confirmed facts, with [source] citations and relevant numbers/constraints
6. **Coherence Check** — show how the parts fit together and resolve tensions
7. **Uncertainty** — material uncertainty, confidence, and what remains unstable
8. **Knowledge Integration** — connect facts, constraints, and domain knowledge
   into a robust answer; this is about robustness of the answer itself
9. **Reflection** — reasoning-level self-assessment, not domain hedging. Include:
   alternative framings: 1-2 frames considered and rejected, and how the conclusion
   would change if a rejected frame were correct; calibrated confidence: 2-3 load-
   bearing claims, each with confidence and exactly where it may be wrong; method
   or pipeline bias: biases introduced by the evidence-generation method itself
   (for example source-type skew, supply-vs-demand skew, measurement/proxy skew),
   not only domain uncertainty; weakest link and highest-leverage gap: the weakest
   reasoning link and the one item of work or information most likely to change
   the conclusion; residual risks if acting on the answer as written.

Stay factual and grounded in the provided facts. The answer must remain
committed regardless of the question's domain. Use neutral, substantive language;
do not use hedging as a substitute for analysis.
"""

_CRITIQUE_SYSTEM = """\
You are a strict critic of a final-answer draft. Evaluate whether the draft
maximizes these dimensions without weakening commitment: framing, decomposition,
diversity of candidate views, coherence, justification, uncertainty, knowledge
integration, and metacognition.

Metacognition is a hard gate. Explicitly inspect section 9, **Reflection**,
against this checklist:
- It must be about the reasoning process, not merely reflection about the domain.
- It must include rejected alternative framings and how the conclusion would
  change if one were correct.
- It must calibrate confidence for each of 2-3 load-bearing claims and state
  where each claim may be wrong.
- It must identify method/pipeline biases introduced by the way evidence was
  generated or selected.
- It must name the weakest reasoning link, the highest-leverage missing
  information/work, and residual action risks.

Flag any failure as a metacognition defect. Do not relax the other dimensions;
the answer must remain committed, direct, and evidence-grounded.

Return concise, actionable critique bullets only.
"""

_REFINE_SYSTEM = """\
You are refining the final answer after critique. Preserve the committed direct
answer and improve the draft against the critique.

If the critique flags metacognition, deepen section 9, **Reflection**, by adding
the missing checklist elements: alternative framings, calibrated confidence for
load-bearing claims, method/pipeline bias, weakest link plus highest-leverage gap,
and residual risks. Do not turn this into domain hedging or a refusal.

Do not trim the other eight sections; this is a Pareto improvement, not a
rebalancing. Keep framing, decomposition, diversity, coherence, justification,
uncertainty, and knowledge integration at least as strong as in the draft.

Return only the final markdown answer with all nine sections.
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

Produce the final answer now with all nine required sections. Lead with the direct,
concrete answer to the question. Commit to a single best-supported conclusion;
do not defer or hedge.
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

Critique the draft. Treat metacognition as a strict gate: if **Reflection** is
domain reflection rather than reasoning-level self-assessment, lacks rejected
framings, lacks calibrated confidence for each load-bearing claim, lacks
method/pipeline bias, or lacks weakest-link/highest-leverage-gap analysis, flag
it explicitly. Do not weaken the other seven rubric dimensions or the committed
Direct Answer.
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

Refine the draft into the final answer. If metacognition was flagged, deepen
only section 9, **Reflection**, with the missing reasoning-level elements. Do
not trim the other eight sections; this is a Pareto improvement, not a
rebalancing. Keep the Direct Answer committed and avoid fail-closed language.
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
        "## Direct Answer\n",
        f"{best_claim}\n",
        "## Framing\n",
        "The answer is framed as selecting the strongest committed conclusion "
        f"for: {question}\n",
        "## Decomposition\n",
        "- Identify the highest-confidence facts.\n"
        "- Check contradictions.\n"
        "- Commit to the best-supported conclusion.\n",
        "## Candidate Views\n",
        "- Primary view: follow the strongest confirmed fact pattern.\n"
        "- Alternative view: give more weight to unresolved contradictions if "
        "they directly undermine the main claim.\n",
        "## Evidence And Justification\n",
    ]
    lines.extend(fact_lines or ["- No confirmed facts were available."])
    lines.extend([
        "\n## Coherence Check\n",
        "The conclusion follows the highest-confidence available claims while "
        "keeping contradictions visible rather than letting them erase the answer.\n",
        "## Uncertainty\n",
        f"Overall confidence is {overall_conf:.2f}. Contradictions and "
        "low-confidence facts remain the main uncertainty drivers.\n",
        "## Knowledge Integration\n",
        "The answer integrates the confirmed claims, source agreement, confidence "
        "scores, and contradiction status into one committed conclusion.\n",
        "## Reflection\n",
        "- Alternative framings: A conservative evidence-only frame would withhold "
        "judgment, but the final-answer frame requires the best-supported committed "
        "conclusion. A contradiction-first frame would change the answer only if "
        "unresolved contradictions directly defeat the highest-confidence claim.",
        f"- Calibrated confidence: The main conclusion has confidence {overall_conf:.2f}; "
        "it may be wrong where the top confirmed fact is incomplete, stale, or "
        "source-skewed. The contradiction assessment is moderate-confidence unless "
        "all conflicting claims share comparable source quality. The source-weighting "
        "step is weaker if agent outputs used overlapping sources.",
        "- Method/pipeline bias: The fallback uses available confirmed facts and "
        "confidence scores, so it may inherit source-type skew, agent-selection skew, "
        "and over-weighting of easily retrieved evidence.",
        "- Weakest link and highest-leverage gap: The weakest link is whether the "
        "highest-confidence fact captures the decisive constraint. The highest-leverage "
        "next check is an independent source or computation aimed directly at that "
        "decisive constraint.",
        "- Residual risks: Acting as written may miss edge cases, minority evidence, "
        "or late-breaking facts not captured in the confirmed-fact set.\n",
        "## Unresolved Contradictions\n",
    ])
    lines.extend(contradiction_lines)

    return "\n".join(lines)
