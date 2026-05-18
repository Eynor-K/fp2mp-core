"""
LLM-Wiki maintenance: pruning, merging, and relevance scoring.

Runs after every WikiCurator round to keep the wiki compact and relevant.
"""

from __future__ import annotations

import re

from fp2mp_core.config import get_settings
from fp2mp_core.state import SubQuery, WikiPage
from fp2mp_core.text_utils import jaccard as _jaccard

_PROTECTED = {"index.md", "log.md", "redi_fusion", "synthesis", "task_context"}

_s = get_settings()
_PRUNE_CONFIDENCE_THRESHOLD = _s.prune_confidence_threshold
_PRUNE_MIN_ITERATIONS = _s.prune_min_iterations
_MERGE_JACCARD_THRESHOLD = _s.merge_jaccard_threshold


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def compute_relevance_scores(
    wiki: dict[str, WikiPage],
    sub_queries: list[SubQuery],
) -> dict[str, WikiPage]:
    """
    Assign relevance_score = incoming_cross_refs_count × confidence × sq_match_bonus.
    sq_match_bonus = 1.5 if page tags intersect with any sub-query intent_aspect.
    """
    intent_aspects = {sq.get("intent_aspect", "") for sq in sub_queries if sq.get("intent_aspect")}

    updated = {}
    for page_id, page in wiki.items():
        if page_id in _PROTECTED:
            updated[page_id] = page
            continue

        n_refs = len(page.get("incoming_cross_refs", []))
        confidence = page.get("confidence", 0.0)
        tags = set(page.get("tags", []))
        sq_match = bool(tags & intent_aspects)
        bonus = 1.5 if sq_match else 1.0
        score = (n_refs + 1) * confidence * bonus   # +1 so new pages aren't immediately zero

        p = dict(page)
        p["relevance_score"] = round(score, 4)
        updated[page_id] = WikiPage(**p)  # type: ignore[arg-type]

    return updated


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def prune_wiki(
    wiki: dict[str, WikiPage],
    sub_queries: list[SubQuery],
    current_iteration: int,
) -> dict[str, WikiPage]:
    """
    Remove pages that are irrelevant AND low-confidence AND isolated.

    A page is pruned if ALL of:
    - Not protected
    - confidence < _PRUNE_CONFIDENCE_THRESHOLD
    - no incoming cross-refs from other pages
    - tags do not intersect any sub-query intent_aspect
    - page has existed for at least _PRUNE_MIN_ITERATIONS
    """
    intent_aspects = {sq.get("intent_aspect", "") for sq in sub_queries if sq.get("intent_aspect")}

    to_remove: set[str] = set()
    for page_id, page in wiki.items():
        if page_id in _PROTECTED:
            continue

        age = current_iteration - page.get("updated_at_iteration", current_iteration)
        if age < _PRUNE_MIN_ITERATIONS:
            continue

        low_conf = page.get("confidence", 1.0) < _PRUNE_CONFIDENCE_THRESHOLD
        isolated = len(page.get("incoming_cross_refs", [])) == 0
        irrelevant = not bool(set(page.get("tags", [])) & intent_aspects)

        if low_conf and isolated and irrelevant:
            to_remove.add(page_id)

    if not to_remove:
        return wiki

    pruned = {pid: page for pid, page in wiki.items() if pid not in to_remove}

    # Clean up dangling cross-refs in remaining pages
    ref_pattern = re.compile(r"\[\[(" + "|".join(re.escape(r) for r in to_remove) + r")\]\]")
    cleaned: dict[str, WikiPage] = {}
    for page_id, page in pruned.items():
        content = page.get("content", "")
        new_content = ref_pattern.sub(r"\1", content)   # strip [[]] around removed page ids
        if new_content != content:
            p = dict(page)
            p["content"] = new_content
            cleaned[page_id] = WikiPage(**p)  # type: ignore[arg-type]
        else:
            cleaned[page_id] = page

    return cleaned


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def merge_overlapping_pages(
    wiki: dict[str, WikiPage],
    iteration: int,
) -> tuple[dict[str, WikiPage], list[str]]:
    """
    Merge pairs of pages whose content is highly similar (Jaccard ≥ threshold).
    Returns (updated_wiki, list_of_merge_summaries_for_log).
    """
    page_ids = [pid for pid in wiki if pid not in _PROTECTED]
    merged_into: dict[str, str] = {}  # old_id → new_merged_id
    merge_log: list[str] = []

    result = dict(wiki)

    for i, id_a in enumerate(page_ids):
        if id_a in merged_into:
            continue
        for id_b in page_ids[i + 1 :]:
            if id_b in merged_into or id_a in merged_into:
                continue
            page_a = result.get(id_a)
            page_b = result.get(id_b)
            if not page_a or not page_b:
                continue

            sim = _jaccard(page_a.get("content", ""), page_b.get("content", ""))
            if sim < _MERGE_JACCARD_THRESHOLD:
                continue

            # Merge B into A
            merged_content = (
                page_a.get("content", "")
                + f"\n\n---\n*Merged from [[{id_b}]] at iteration {iteration}*\n\n"
                + page_b.get("content", "")
            )

            # Deduplicate citations by URL
            all_cits = page_a.get("citations", []) + page_b.get("citations", [])
            seen: set[str] = set()
            deduped_cits = []
            for c in all_cits:
                url = c.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    deduped_cits.append(c)
                elif not url:
                    deduped_cits.append(c)

            merged_tags = list(set(page_a.get("tags", []) + page_b.get("tags", [])))
            merged_refs = list(
                set(page_a.get("incoming_cross_refs", []) + page_b.get("incoming_cross_refs", []))
            )
            merged_conf = max(page_a.get("confidence", 0.0), page_b.get("confidence", 0.0))

            merged_page = WikiPage(
                page_id=id_a,
                title=page_a["title"],
                content=merged_content,
                updated_by="WikiCurator",
                updated_at_iteration=iteration,
                confidence=merged_conf,
                citations=deduped_cits,
                tags=merged_tags,
                incoming_cross_refs=merged_refs,
                relevance_score=page_a.get("relevance_score", 0.0),
            )

            result[id_a] = merged_page
            merged_into[id_b] = id_a
            if id_b in result:
                del result[id_b]

            # Update all cross-refs in remaining pages: [[id_b]] → [[id_a]]
            for pid in list(result.keys()):
                if pid == id_a:
                    continue
                pg = result[pid]
                old_content = pg.get("content", "")
                new_content = old_content.replace(f"[[{id_b}]]", f"[[{id_a}]]")
                if new_content != old_content:
                    p = dict(pg)
                    p["content"] = new_content
                    result[pid] = WikiPage(**p)  # type: ignore[arg-type]

            merge_log.append(f"Merged {id_b} → {id_a} (Jaccard={sim:.2f})")

    return result, merge_log
