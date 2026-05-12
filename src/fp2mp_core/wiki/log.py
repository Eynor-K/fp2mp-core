"""
LLM-Wiki log.md — append-only audit trail of all wiki operations.
Stored as wiki["log.md"].content.
"""

from __future__ import annotations

from fp2mp_core.state import LogEntry, WikiPage, now_iso


def make_log_entry(
    iteration: int,
    agent: str,
    action: str,
    page_id: str,
    summary: str,
) -> LogEntry:
    return LogEntry(
        timestamp=now_iso(),
        iteration=iteration,
        agent=agent,
        action=action,
        page_id=page_id,
        summary=summary,
    )


def append_log_entry(current_log_content: str, entry: LogEntry) -> str:
    """Append a structured entry to the log.md content string."""
    line = (
        f"\n## [{entry['iteration']}] [{entry['timestamp']}] "
        f"[{entry['agent']}] {entry['action']}: {entry['page_id']}\n"
        f"{entry['summary']}\n"
    )
    return current_log_content + line


def make_initial_log_page() -> WikiPage:
    return WikiPage(
        page_id="log.md",
        title="Change Log",
        content="# Change Log\n",
        updated_by="system",
        updated_at_iteration=0,
        confidence=1.0,
        citations=[],
        tags=["system"],
        incoming_cross_refs=[],
        relevance_score=1.0,
    )
