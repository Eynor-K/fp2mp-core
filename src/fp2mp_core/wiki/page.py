"""
WikiPageBuilder: converts RawEntry items into structured LLM-Wiki pages.

Features:
- Cross-reference injection: scans content for mentions of other page titles → [[page_id]]
- Conflict detection: flags contradictions between new and existing content with > CONFLICT:
- Relevance scoring: computed from incoming cross-refs and sub-query tag match
"""

from __future__ import annotations

import re
import uuid

from fp2mp_core.state import Citation, RawEntry, SubQuery, WikiPage

_PROTECTED_PAGES = {"index.md", "log.md", "redi_fusion", "synthesis", "task_context"}
_CONFLICT_THRESHOLD = 0.25  # Jaccard below this → potential contradiction if same aspect


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _inject_cross_refs(content: str, all_pages: dict[str, WikiPage]) -> str:
    """Replace mentions of known page titles with [[page_id]] markers."""
    for page_id, page in all_pages.items():
        if page_id in _PROTECTED_PAGES:
            continue
        title = page.get("title", "")
        if title and len(title) > 4 and title.lower() in content.lower():
            # Only inject if not already a cross-ref
            if f"[[{page_id}]]" not in content:
                content = re.sub(
                    re.escape(title),
                    f"{title} [[{page_id}]]",
                    content,
                    count=1,
                    flags=re.IGNORECASE,
                )
    return content


def _detect_conflict(new_content: str, existing_content: str) -> bool:
    """Simple heuristic: low Jaccard on non-trivial content → possible contradiction."""
    if len(new_content) < 50 or len(existing_content) < 50:
        return False
    return _jaccard(new_content, existing_content) < _CONFLICT_THRESHOLD


def _agent_slug(agent: str) -> str:
    return agent.lower().replace("agent", "").replace(" ", "_").strip("_")


class WikiPageBuilder:
    def build(
        self,
        entry: RawEntry,
        sub_query: SubQuery | None,
        existing_pages: dict[str, WikiPage],
        iteration: int,
    ) -> WikiPage:
        """Build or update a wiki page from a single RawEntry."""
        agent = entry.get("agent", "unknown")
        sq_id = entry.get("sub_query_id", "")
        page_id = f"{_agent_slug(agent)}_{sq_id}" if sq_id else _agent_slug(agent)

        sq_text = sub_query.get("text", "") if sub_query else ""
        title = f"{agent}: {sq_text[:60]}" if sq_text else f"{agent} findings"

        content = entry.get("content", "")

        # Cross-ref injection
        content = _inject_cross_refs(content, existing_pages)

        # Conflict detection against existing page
        conflict_note = ""
        existing = existing_pages.get(page_id)
        if existing and _detect_conflict(content, existing.get("content", "")):
            conflict_note = (
                f"\n\n> CONFLICT: New findings from {agent} (iter {iteration}) "
                f"appear to contradict previous content on this page.\n"
            )

        full_content = content + conflict_note

        tags = [agent]
        if sub_query:
            tags.append(sub_query.get("intent_aspect", ""))
            tags.append(sub_query.get("search_modality", ""))
        tags = [t for t in tags if t]

        citations: list[Citation] = entry.get("citations", [])

        return WikiPage(
            page_id=page_id,
            title=title,
            content=full_content,
            updated_by=agent,
            updated_at_iteration=iteration,
            confidence=entry.get("confidence", 0.0),
            citations=citations,
            tags=list(set(tags)),
            incoming_cross_refs=[],   # populated by update_incoming_cross_refs()
            relevance_score=0.0,      # populated by compute_relevance_scores()
        )

    def update_page(
        self,
        existing: WikiPage,
        entry: RawEntry,
        all_pages: dict[str, WikiPage],
        iteration: int,
    ) -> WikiPage:
        """Append new findings to an existing page."""
        agent = entry.get("agent", "?")
        new_content = entry.get("content", "")
        new_content = _inject_cross_refs(new_content, all_pages)

        conflict_note = ""
        if _detect_conflict(new_content, existing.get("content", "")):
            conflict_note = (
                f"\n\n> CONFLICT (iter {iteration}): {agent} findings "
                "contradict earlier content on this page.\n"
            )

        updated_content = (
            existing.get("content", "")
            + f"\n\n---\n*Updated by {agent} at iteration {iteration}*\n\n"
            + new_content
            + conflict_note
        )

        new_citations = existing.get("citations", []) + entry.get("citations", [])
        # Deduplicate citations by URL
        seen_urls: set[str] = set()
        deduped_citations: list[Citation] = []
        for c in new_citations:
            url = c.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                deduped_citations.append(c)
            elif not url:
                deduped_citations.append(c)

        new_confidence = max(existing.get("confidence", 0.0), entry.get("confidence", 0.0))
        new_tags = list(set(existing.get("tags", []) + [agent]))

        return WikiPage(
            page_id=existing["page_id"],
            title=existing["title"],
            content=updated_content,
            updated_by=agent,
            updated_at_iteration=iteration,
            confidence=new_confidence,
            citations=deduped_citations,
            tags=new_tags,
            incoming_cross_refs=existing.get("incoming_cross_refs", []),
            relevance_score=existing.get("relevance_score", 0.0),
        )


def update_incoming_cross_refs(wiki: dict[str, WikiPage]) -> dict[str, WikiPage]:
    """Rebuild incoming_cross_refs for all pages based on [[page_id]] markers in content."""
    incoming: dict[str, list[str]] = {pid: [] for pid in wiki}
    pattern = re.compile(r"\[\[([^\]]+)\]\]")
    for source_id, page in wiki.items():
        for match in pattern.finditer(page.get("content", "")):
            target_id = match.group(1)
            if target_id in incoming and target_id != source_id:
                if source_id not in incoming[target_id]:
                    incoming[target_id].append(source_id)

    updated = {}
    for pid, page in wiki.items():
        updated_page = dict(page)
        updated_page["incoming_cross_refs"] = incoming[pid]
        updated[pid] = WikiPage(**updated_page)  # type: ignore[arg-type]
    return updated
