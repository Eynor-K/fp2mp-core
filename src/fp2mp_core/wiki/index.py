"""
LLM-Wiki index.md — content-oriented catalog of all wiki pages.
Stored as wiki["index.md"].content and rebuilt after each curation round.
"""

from __future__ import annotations

from fp2mp_core.state import WikiPage

_PROTECTED = {"index.md", "log.md"}

_HEADER = """\
# Knowledge Index

| page_id | title | agent | confidence | relevance | updated_at |
|---------|-------|-------|-----------|-----------|------------|
"""


def build_index(wiki: dict[str, WikiPage], iteration: int) -> str:
    """Generate index.md content from current wiki state."""
    rows: list[str] = [_HEADER]
    for page_id, page in sorted(wiki.items()):
        if page_id in _PROTECTED:
            continue
        title = page.get("title", "")[:50]
        agent = page.get("updated_by", "?")
        conf = f"{page.get('confidence', 0.0):.2f}"
        rel = f"{page.get('relevance_score', 0.0):.2f}"
        upd = f"iter_{page.get('updated_at_iteration', 0)}"
        rows.append(f"| {page_id} | {title} | {agent} | {conf} | {rel} | {upd} |")

    rows.append(f"\n*Generated at iteration {iteration}. Total pages: {len(rows) - 1}*")
    return "\n".join(rows)


def parse_index(text: str) -> dict[str, dict]:
    """Parse index.md back into a dict keyed by page_id (for round-trip tests)."""
    result: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| page_id") or line.startswith("|---"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 5:
            result[parts[0]] = {
                "title": parts[1],
                "agent": parts[2],
                "confidence": float(parts[3]) if parts[3] else 0.0,
                "relevance_score": float(parts[4]) if parts[4] else 0.0,
            }
    return result
