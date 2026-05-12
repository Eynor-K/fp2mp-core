"""
Optional disk persistence for LLM-Wiki pages.
Called from final_synthesis_node if WIKI_PERSIST_DIR is configured.
"""

from __future__ import annotations

import json
from pathlib import Path

from fp2mp_core.state import WikiPage


def persist_wiki(wiki: dict[str, WikiPage], persist_dir: Path) -> None:
    persist_dir.mkdir(parents=True, exist_ok=True)
    for page_id, page in wiki.items():
        safe_name = page_id.replace("/", "_").replace("\\", "_")
        md_path = persist_dir / f"{safe_name}.md"
        meta_path = persist_dir / f"{safe_name}.meta.json"

        md_path.write_text(page.get("content", ""), encoding="utf-8")
        meta = {k: v for k, v in page.items() if k != "content"}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def load_wiki(persist_dir: Path) -> dict[str, WikiPage]:
    if not persist_dir.exists():
        return {}

    wiki: dict[str, WikiPage] = {}
    for meta_path in persist_dir.glob("*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            page_id = meta.get("page_id", meta_path.stem.replace(".meta", ""))
            safe_name = page_id.replace("/", "_").replace("\\", "_")
            md_path = persist_dir / f"{safe_name}.md"
            content = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            meta["content"] = content
            wiki[page_id] = WikiPage(**meta)  # type: ignore[arg-type]
        except Exception:
            continue
    return wiki
