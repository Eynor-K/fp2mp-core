"""Shared failure detection for agent outputs and raw entries.

Prevents silent degradation: a run that errored, hit an iteration/time limit,
or produced no real answer must be marked as failed (low confidence) rather
than surfacing as a plausible mid-confidence finding.
"""

from __future__ import annotations

from typing import Any

# A run that begins with one of these never produced a usable result.
_ERROR_PREFIXES = (
    "[failed]",
    "ошибка",
    "error:",
    "exception:",
    "failed:",
    "code execution failed",
    "search failed",
    "normative search failed",
    "blocksnetagent failed",
)

# The agent never finished — there is no synthesized final answer at all.
_HARD_FAILURE_MARKERS = (
    "agent stopped due to",
    "iteration limit",
    "time limit",
)

# A tool-level error occurred somewhere; recoverable only if a later step succeeded.
_TOOL_ERROR_MARKERS = (
    "no matching signature found",
    "traceback (most recent call last)",
    "security check failed",
    "execution timed out",
    "subprocess error",
    "e2b error",
)


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def text_hard_failure(text: str) -> bool:
    """True when the agent produced no real final answer (empty / stopped)."""
    low = _norm(text)
    if not low:
        return True
    return any(m in low for m in _HARD_FAILURE_MARKERS)


def text_indicates_failure(text: str) -> bool:
    """True when the text is an error message or a known failure marker."""
    low = _norm(text)
    if not low:
        return True
    if any(low.startswith(p) for p in _ERROR_PREFIXES):
        return True
    return any(m in low for m in _HARD_FAILURE_MARKERS + _TOOL_ERROR_MARKERS)


def _steps(tool_trace: list[dict[str, Any]] | None):
    for block in tool_trace or []:
        for step in block.get("intermediate_steps") or []:
            yield step


def trace_has_successful_step(tool_trace: list[dict[str, Any]] | None) -> bool:
    for step in _steps(tool_trace):
        obs = str(step.get("observation", "")).strip()
        if obs and not text_indicates_failure(obs):
            return True
    return False


def trace_has_tool_error(tool_trace: list[dict[str, Any]] | None) -> bool:
    for step in _steps(tool_trace):
        obs = str(step.get("observation", "")).strip()
        if obs and text_indicates_failure(obs):
            return True
    return False


def assess_confidence(
    output_text: str,
    tool_trace: list[dict[str, Any]] | None,
    parsed_confidence: float,
) -> tuple[float, bool]:
    """Return (confidence, failed), downgrading silent failures.

    - Hard failure (agent stopped / empty): confidence 0.2, failed.
    - Error output with no salvageable tool step: confidence 0.2, failed.
    - Tool error with no later successful step: cap at 0.25, failed.
    - Otherwise: keep the agent's parsed confidence.
    """
    if text_hard_failure(output_text):
        return 0.2, True
    if text_indicates_failure(output_text) and not trace_has_successful_step(tool_trace):
        return 0.2, True
    if trace_has_tool_error(tool_trace) and not trace_has_successful_step(tool_trace):
        return min(parsed_confidence, 0.25), True
    return parsed_confidence, False


def entry_failed(entry: dict[str, Any]) -> bool:
    """True when a RawEntry should be treated as a failed attempt."""
    if entry.get("confidence", 1.0) < 0.4:
        return True
    if text_indicates_failure(entry.get("content", "")):
        return True
    tool_trace = entry.get("tool_trace") or []
    if tool_trace and trace_has_tool_error(tool_trace) and not trace_has_successful_step(tool_trace):
        return True
    return False
