"""Pytest entrypoint for the benchmark smoke preflight helpers."""

from __future__ import annotations

from tests.benchmark_smoke import preflight_answer


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
