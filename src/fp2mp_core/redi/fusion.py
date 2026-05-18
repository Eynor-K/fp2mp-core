"""
ReDI Stage C: Additive fusion of raw_data entries per sub-query.

Deduplicates by token-level Jaccard similarity, scores findings by keyword overlap,
and returns a fusion summary placed into wiki["redi_fusion"].
"""

from __future__ import annotations

from collections import defaultdict

from fp2mp_core.state import RawEntry, SubQuery, WikiPage
from fp2mp_core.text_utils import jaccard as _jaccard
from fp2mp_core.text_utils import keyword_overlap as _keyword_overlap


def _deduplicate(entries: list[RawEntry], threshold: float = 0.7) -> list[RawEntry]:
    """Remove near-duplicate entries (Jaccard ≥ threshold); keep higher confidence."""
    unique: list[RawEntry] = []
    for entry in sorted(entries, key=lambda e: e.get("confidence", 0.0), reverse=True):
        content = entry.get("content", "")
        if any(_jaccard(content, u.get("content", "")) >= threshold for u in unique):
            continue
        unique.append(entry)
    return unique


def _score(entry: RawEntry, sub_query: SubQuery) -> float:
    kw_score = _keyword_overlap(entry.get("content", ""), sub_query.get("keywords", []))
    confidence = entry.get("confidence", 0.5)
    return kw_score * confidence


class ReDIFusion:
    def __init__(
        self,
        dedup_threshold: float = 0.7,
        top_n: int = 5,
    ) -> None:
        self._dedup_threshold = dedup_threshold
        self._top_n = top_n

    def fuse(
        self,
        raw_data: list[RawEntry],
        sub_queries: list[SubQuery],
    ) -> WikiPage:
        """
        For each sub-query, deduplicate entries and produce additive fusion summary.
        Returns a WikiPage to be stored as wiki["redi_fusion"].
        """
        by_sq: dict[str, list[RawEntry]] = defaultdict(list)
        for entry in raw_data:
            sq_id = entry.get("sub_query_id", "")
            if sq_id:
                by_sq[sq_id].append(entry)

        sq_index = {sq["sub_query_id"]: sq for sq in sub_queries}
        sections: list[str] = ["# ReDI Fusion Summary\n"]

        for sq_id, entries in by_sq.items():
            sq = sq_index.get(sq_id)
            if not sq:
                continue

            deduped = _deduplicate(entries, self._dedup_threshold)
            scored = sorted(
                [(e, _score(e, sq)) for e in deduped],
                key=lambda x: x[1],
                reverse=True,
            )
            top = scored[: self._top_n]

            additive_score = sum(s for _, s in top)

            sections.append(f"\n## {sq_id}: {sq.get('text', '')}")
            sections.append(f"Additive score: {additive_score:.3f} | Unique findings: {len(deduped)}\n")
            for i, (entry, score) in enumerate(top, 1):
                agent = entry.get("agent", "?")
                conf = entry.get("confidence", 0.0)
                snippet = entry.get("content", "")[:200].replace("\n", " ")
                sections.append(f"{i}. [{agent} conf={conf:.2f} score={score:.3f}] {snippet}")

        content = "\n".join(sections)
        return WikiPage(
            page_id="redi_fusion",
            title="ReDI Fusion Summary",
            content=content,
            updated_by="ReDIFusion",
            updated_at_iteration=0,
            confidence=0.0,
            citations=[],
            tags=["fusion", "redi"],
            incoming_cross_refs=[],
            relevance_score=1.0,
        )
