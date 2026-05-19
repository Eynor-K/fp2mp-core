"""Cheap structural preflight for benchmark-style final answers.

These checks do not replace the LLM judge. They catch obvious regressions before
running the expensive `fp2mp-eval` panel.
"""

from __future__ import annotations

REQUIRED_SECTIONS = (
    "Direct Answer",
    "Framing",
    "Decomposition",
    "Candidate Views",
    "Evidence And Justification",
    "Coherence Check",
    "Uncertainty",
    "Knowledge Integration",
    "Reflection",
)

REQUIRED_SIGNALS = (
    "alternative",
    "confidence",
    "trade-off",
    "risk",
    "indicator",
    "source",
)


def preflight_answer(answer: str) -> list[str]:
    """Return structural benchmark-readiness issues for a final answer."""
    issues: list[str] = []
    lower = answer.lower()

    for section in REQUIRED_SECTIONS:
        if section.lower() not in lower:
            issues.append(f"missing section: {section}")

    for signal in REQUIRED_SIGNALS:
        if signal not in lower:
            issues.append(f"missing signal: {signal}")

    if lower.count("alternative") < 2:
        issues.append("fewer than two alternative/candidate mentions")
    if lower.count("confidence") < 2:
        issues.append("fewer than two confidence calibrations")

    return issues


def test_benchmark_preflight_accepts_rubric_ready_answer():
    answer = """
## Direct Answer
Choose option A with a staged implementation plan [source].
## Framing
This is a decision under constraints with measurable success indicators.
## Decomposition
Break the problem into demand, constraints, costs, and implementation.
## Candidate Views
Alternative 1 prioritizes speed; alternative 2 prioritizes resilience.
The chosen path balances the trade-off between both.
## Evidence And Justification
The source evidence supports option A and defines one indicator.
## Coherence Check
The parts fit because the same constraint drives the chosen option and risk controls.
## Uncertainty
Confidence is high for the main direction and moderate for cost timing.
## Knowledge Integration
The answer integrates source facts, constraints, and option comparison.
## Reflection
Alternative framing could change the choice. Confidence in the load-bearing claim
may be wrong if source coverage is skewed. Pipeline bias may over-weight retrieved
sources. The weakest link is the missing implementation test. Residual risk remains.
"""

    assert preflight_answer(answer) == []


def test_benchmark_preflight_flags_missing_rubric_elements():
    issues = preflight_answer("## Direct Answer\nDo this.\n")

    assert "missing section: Reflection" in issues
    assert "missing signal: trade-off" in issues
