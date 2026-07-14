"""Shared heuristics for classifying agent outputs as failed or usable."""

from __future__ import annotations

from collections.abc import Iterable

from fp2mp_core.state import RawEntry

_FAILED_CONTENT_PREFIXES: tuple[str, ...] = (
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

_SKIPPED_TYPES: frozenset[str] = frozenset(
    {"init", "orchestrator_directive", "critique", "curator_summary"}
)


def is_failed_claim_text(text: str) -> bool:
    """Return True if ``text`` looks like a failure marker."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    return stripped.startswith(_FAILED_CONTENT_PREFIXES)


def is_failed_entry(entry: RawEntry) -> bool:
    """Return True if ``entry`` represents a failed agent output."""
    return is_failed_claim_text(entry.get("content", ""))


def is_curatable(entry: RawEntry) -> bool:
    """Return True if ``entry`` is eligible for downstream use."""
    return entry.get("type") not in _SKIPPED_TYPES and not is_failed_entry(entry)


def usable_entries(
    entries: Iterable[RawEntry],
    *,
    current_iteration: int | None = None,
    skip_system: bool = True,
) -> list[RawEntry]:
    """Return ``entries`` filtered by iteration, type and failure status."""
    result: list[RawEntry] = []
    for entry in entries:
        if skip_system and entry.get("type") in _SKIPPED_TYPES:
            continue
        if current_iteration is not None and entry.get("iteration", 0) > current_iteration:
            continue
        if is_failed_entry(entry):
            continue
        result.append(entry)
    return result